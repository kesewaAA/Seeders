"""Seeders — Flask application factory and routes."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date
from functools import wraps
from typing import Callable

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from models import (Availability, Movie, User, UserMovieStatus, UserPreference,
                    UserRating, VALID_STATUSES, db, get_or_create_preference,
                    is_availability_stale, upsert_availability,
                    upsert_movie_from_tmdb)
from streaming import StreamingError, canonical_platform, get_availability_by_tmdb_id
from tmdb import (TMDbError, backdrop_url, discover, discover_recent_popular,
                  genre_map, movie_detail, poster_url, search_movies,
                  trending_week)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> Flask:
    load_dotenv()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///movies.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    # db.create_all() picks up new tables (Availability, UserPreference) on next
    # run without any migration tool.  For schema-altering changes on existing
    # tables, Alembic would be needed — adding that is a separate task.
    with app.app_context():
        db.create_all()

    _register_routes(app)
    _register_template_helpers(app)
    _register_context(app)
    return app


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def current_user() -> User | None:
    uid = session.get("user_id")
    if not uid:
        return None
    if getattr(g, "_user", None) is None or g._user.id != uid:
        g._user = db.session.get(User, uid)
    return g._user


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Preference helper
# ---------------------------------------------------------------------------


def _user_prefs(user: User | None) -> tuple[float, bool, list[str]]:
    """Return (min_rating, free_only, platform_list) for the current user.

    Always safe — returns defaults when the user is None or has no preference
    row yet (user_preferences table is empty for new accounts).
    """
    if user is None:
        return 0.0, False, []
    pref = UserPreference.query.filter_by(user_id=user.id).one_or_none()
    if pref is None:
        return 0.0, False, []
    return pref.min_rating, pref.free_only, pref.platform_list()


def _availability_map(movie_ids: list[int]) -> dict[int, list[Availability]]:
    """Bulk-load cached availability rows for a list of movie primary-key IDs."""
    if not movie_ids:
        return {}
    rows = Availability.query.filter(Availability.movie_id.in_(movie_ids)).all()
    result: dict[int, list[Availability]] = {}
    for row in rows:
        result.setdefault(row.movie_id, []).append(row)
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: Flask) -> None:

    @app.route("/")
    def index():
        trending, recent = [], []
        error = None
        try:
            trending = trending_week()[:12]
            recent = discover_recent_popular()[:12]
            for payload in trending + recent:
                upsert_movie_from_tmdb(payload)
            db.session.commit()
        except TMDbError as exc:
            error = str(exc)
        return render_template(
            "index.html", trending=trending, recent=recent, error=error,
        )

    # --- Search ------------------------------------------------------------

    @app.route("/search")
    def search():
        q = (request.args.get("q") or "").strip()

        # Query-param overrides take priority; fall back to saved preference.
        user = current_user()
        saved_min, saved_free, saved_platforms = _user_prefs(user)
        try:
            min_rating = float(request.args["min_rating"]) if "min_rating" in request.args else saved_min
        except ValueError:
            min_rating = saved_min
        free_only = request.args.get("free_only") == "1" if "free_only" in request.args else saved_free
        selected_platforms = request.args.getlist("platform") or saved_platforms

        results, error = [], None
        if q:
            try:
                raw = search_movies(q)
                for payload in raw:
                    upsert_movie_from_tmdb(payload)
                db.session.commit()
            except TMDbError as exc:
                error = str(exc)
                raw = []

            # Resolve to DB rows so we can filter by rating and join availability.
            tmdb_ids = [p["id"] for p in raw if p.get("id")]
            db_movies = {m.tmdb_id: m for m in
                         Movie.query.filter(Movie.tmdb_id.in_(tmdb_ids)).all()}
            avail_map = _availability_map([m.id for m in db_movies.values()])

            # Fail-open guard: skip availability filters when the cache is empty
            # so an unpopulated table doesn't hide the entire catalog.
            # Run `python fetch_availability.py` once to populate the cache.
            availability_seeded = db.session.query(Availability.id).first() is not None

            for payload in raw:
                m = db_movies.get(payload["id"])
                if m is None:
                    continue
                if min_rating and (m.tmdb_rating or 0.0) < min_rating:
                    continue
                avail = avail_map.get(m.id, [])
                if availability_seeded:
                    if free_only and not _has_free(avail):
                        continue
                    if selected_platforms and not _matches_platforms(avail, selected_platforms):
                        continue
                results.append((m, avail))

        return render_template(
            "search.html", q=q, results=results, error=error,
            min_rating=min_rating, free_only=free_only,
            selected_platforms=selected_platforms,
        )

    # --- Movie detail ------------------------------------------------------

    @app.route("/movie/<int:tmdb_id>")
    def movie_page(tmdb_id: int):
        payload = None
        error = None
        streaming_error = None
        try:
            payload = movie_detail(tmdb_id)
            movie = upsert_movie_from_tmdb(payload)
            db.session.commit()
        except TMDbError as exc:
            error = str(exc)
            movie = Movie.query.filter_by(tmdb_id=tmdb_id).one_or_none()
            if movie is None:
                abort(404)

        # Refresh availability if stale / absent — failures are silent to the user
        # but always logged to the terminal so errors are visible during development.
        # A data hiccup for one movie (API error OR a DB write failure such as an
        # IntegrityError) must never 500 the detail page — roll back and render
        # without availability instead.
        if is_availability_stale(movie):
            try:
                options = get_availability_by_tmdb_id(tmdb_id)
                upsert_availability(movie.id, options)
                db.session.commit()
            except StreamingError as exc:
                streaming_error = str(exc)
                app.logger.error("StreamingError for tmdb_id=%s: %s", tmdb_id, exc)
            except Exception as exc:
                db.session.rollback()
                streaming_error = "Streaming info unavailable right now."
                app.logger.error("Availability write failed for tmdb_id=%s: %s", tmdb_id, exc)

        availability = Availability.query.filter_by(movie_id=movie.id).all()

        user = current_user()
        user_status = user_rating = None
        if user:
            status_row = UserMovieStatus.query.filter_by(
                user_id=user.id, movie_id=movie.id
            ).one_or_none()
            rating_row = UserRating.query.filter_by(
                user_id=user.id, movie_id=movie.id
            ).one_or_none()
            if status_row:
                user_status = status_row.status
            if rating_row:
                user_rating = rating_row.rating

        genre_names = [genre_map().get(gid, "") for gid in movie.genre_ids()]
        genre_names = [gn for gn in genre_names if gn]

        # Group availability by access_type for the template.
        avail_grouped: dict[str, list[Availability]] = {}
        for row in availability:
            avail_grouped.setdefault(row.access_type, []).append(row)

        return render_template(
            "movie_detail.html",
            movie=movie,
            payload=payload,
            user_status=user_status,
            user_rating=user_rating,
            genre_names=genre_names,
            avail_grouped=avail_grouped,
            streaming_error=streaming_error,
            error=error,
        )

    # --- Status / rating POSTs --------------------------------------------

    @app.route("/movie/<int:tmdb_id>/status", methods=["POST"])
    @login_required
    def set_status(tmdb_id: int):
        user = current_user()
        status = _form_or_json("status")
        if status not in VALID_STATUSES:
            return _json_or_redirect({"error": "invalid status"}, tmdb_id, 400)

        movie = _ensure_movie(tmdb_id)
        row = UserMovieStatus.query.filter_by(
            user_id=user.id, movie_id=movie.id
        ).one_or_none()
        if row is None:
            row = UserMovieStatus(user_id=user.id, movie_id=movie.id, status=status)
            db.session.add(row)
        else:
            row.status = status
        db.session.commit()
        return _json_or_redirect({"ok": True, "status": status}, tmdb_id)

    @app.route("/movie/<int:tmdb_id>/rate", methods=["POST"])
    @login_required
    def rate_movie(tmdb_id: int):
        user = current_user()
        try:
            rating = float(_form_or_json("rating"))
        except (TypeError, ValueError):
            return _json_or_redirect({"error": "invalid rating"}, tmdb_id, 400)
        if not (1.0 <= rating <= 10.0):
            return _json_or_redirect({"error": "rating must be 1-10"}, tmdb_id, 400)

        movie = _ensure_movie(tmdb_id)
        row = UserRating.query.filter_by(
            user_id=user.id, movie_id=movie.id
        ).one_or_none()
        if row is None:
            row = UserRating(user_id=user.id, movie_id=movie.id, rating=rating)
            db.session.add(row)
        else:
            row.rating = rating
        db.session.commit()
        return _json_or_redirect({"ok": True, "rating": rating}, tmdb_id)

    # --- Preferences -------------------------------------------------------

    @app.route("/preferences", methods=["POST"])
    @login_required
    def save_preferences():
        user = current_user()
        pref = get_or_create_preference(user.id)
        try:
            raw_min = _form_or_json("min_rating")
            pref.min_rating = max(0.0, min(10.0, float(raw_min))) if raw_min not in (None, "") else 0.0
        except (TypeError, ValueError):
            pref.min_rating = 0.0

        free_raw = _form_or_json("free_only")
        pref.free_only = free_raw in (True, "1", "true", "on")

        # platforms — repeated form field or JSON list
        if request.is_json:
            platforms = (request.get_json(silent=True) or {}).get("platforms", [])
        else:
            platforms = request.form.getlist("platforms")
        pref.preferred_platforms = json.dumps([p.lower().strip() for p in platforms if p])

        db.session.commit()

        # Redirect back to wherever the user came from.
        next_url = request.args.get("next") or request.referrer or url_for("index")
        return redirect(next_url)

    # --- Watchlist ---------------------------------------------------------

    @app.route("/watchlist")
    @login_required
    def watchlist():
        user = current_user()
        rows = (db.session.query(Movie, UserMovieStatus)
                .join(UserMovieStatus, UserMovieStatus.movie_id == Movie.id)
                .filter(UserMovieStatus.user_id == user.id,
                        UserMovieStatus.status == "want_to_watch")
                .order_by(UserMovieStatus.updated_at.desc())
                .all())
        movies = [m for m, _ in rows]
        return render_template("watchlist.html", movies=movies)

    # --- Recommendations ---------------------------------------------------

    @app.route("/recommendations")
    @login_required
    def recommendations():
        user = current_user()
        saved_min, saved_free, saved_platforms = _user_prefs(user)

        selected_genres = [int(g) for g in request.args.getlist("genre") if g.isdigit()]
        try:
            year_min = int(request.args.get("year_min")) if request.args.get("year_min") else None
        except ValueError:
            year_min = None
        try:
            year_max = int(request.args.get("year_max")) if request.args.get("year_max") else None
        except ValueError:
            year_max = None
        try:
            min_rating = float(request.args["min_rating"]) if "min_rating" in request.args else saved_min
        except ValueError:
            min_rating = saved_min
        free_only = request.args.get("free_only") == "1" if "free_only" in request.args else saved_free
        selected_platforms = request.args.getlist("platform") or saved_platforms

        top_genres = _top_genres_for_user(user.id, limit=3)

        error = None
        try:
            candidates_payload = discover(
                with_genres=selected_genres or None,
                year_min=year_min,
                year_max=year_max,
                min_rating=min_rating if min_rating else None,
            )
            for payload in candidates_payload:
                upsert_movie_from_tmdb(payload)
            if top_genres and not selected_genres:
                extra = discover(with_genres=top_genres)
                for payload in extra:
                    upsert_movie_from_tmdb(payload)
            db.session.commit()
        except TMDbError as exc:
            error = str(exc)

        # Build candidate movies from the local DB after upserts.
        # Materialise once — calling query.all() twice leaves two open cursors
        # on SQLite simultaneously and causes an OperationalError: database is locked.
        q = Movie.query
        if year_min is not None:
            q = q.filter(Movie.release_year >= year_min)
        if year_max is not None:
            q = q.filter(Movie.release_year <= year_max)

        seen_ids = {row.movie_id for row in UserMovieStatus.query
                    .filter_by(user_id=user.id, status="seen").all()}

        selected_set = set(selected_genres)
        all_movies = q.all()
        avail_map = _availability_map([m.id for m in all_movies])

        # Fail-open guard: skip availability filters when the cache is empty
        # so an unpopulated table doesn't hide the entire catalog.
        # Run `python fetch_availability.py` once to populate the cache.
        availability_seeded = db.session.query(Availability.id).first() is not None

        candidates = []
        for m in all_movies:
            if m.id in seen_ids:
                continue
            if (m.tmdb_rating or 0.0) < (min_rating or 0.0):
                continue
            if selected_set and not (selected_set & set(m.genre_ids())):
                continue
            if availability_seeded:
                avail = avail_map.get(m.id, [])
                if free_only and not _has_free(avail):
                    continue
                if selected_platforms and not _matches_platforms(avail, selected_platforms):
                    continue
            candidates.append(m)

        this_year = date.today().year
        scored = []
        for m in candidates:
            score = _score_movie(m, top_genres, this_year)
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        scored = scored[:60]

        return render_template(
            "recommendations.html",
            scored=scored,
            avail_map=avail_map,
            genres=genre_map(),
            selected_genres=selected_genres,
            year_min=year_min,
            year_max=year_max,
            min_rating=min_rating,
            free_only=free_only,
            selected_platforms=selected_platforms,
            top_genres=top_genres,
            error=error,
        )

    # --- Discover (swipe mode) ---------------------------------------------

    @app.route("/discover")
    @login_required
    def discover_page():
        user = current_user()
        min_rating, free_only, _ = _user_prefs(user)

        has_status_ids = {row.movie_id for row in
                          UserMovieStatus.query.filter_by(user_id=user.id).all()}
        dismissed = set(session.get("discover_dismissed", []))

        # Try to refresh the local movie cache from TMDb, but never let a TMDb
        # failure produce an empty queue — fall through to what's already in the DB.
        error = None
        try:
            payload_list = discover_recent_popular(months_back=18)
            for p in payload_list:
                upsert_movie_from_tmdb(p)
            db.session.commit()
        except TMDbError as exc:
            error = str(exc)

        # Candidate pool = ALL movies in the local DB (not just the current
        # TMDb response — seeded movies must also appear here).
        all_movies = Movie.query.order_by(Movie.tmdb_rating.desc().nullslast()).all()
        avail_map = _availability_map([m.id for m in all_movies])
        gmap = genre_map()

        # Fail-open guard: skip availability filters when the cache is empty.
        # Run `python fetch_availability.py` once to populate the cache.
        availability_seeded = db.session.query(Availability.id).first() is not None

        queue = []
        for m in all_movies:
            if m.id in has_status_ids:
                continue
            if m.tmdb_id in dismissed:
                continue
            if (m.tmdb_rating or 0.0) < min_rating:
                continue
            if availability_seeded and free_only and not _has_free(avail_map.get(m.id, [])):
                continue
            genre_labels = [gmap.get(gid, "") for gid in m.genre_ids()[:2]]
            genre_labels = [g for g in genre_labels if g]
            queue.append({
                "tmdb_id": m.tmdb_id,
                "title": m.title,
                "year": m.release_year,
                "rating": m.tmdb_rating,
                "overview": m.overview or "",
                "poster": poster_url(m.poster_path, "w342"),
                "backdrop": backdrop_url(m.backdrop_path, "w1280"),
                "genres": genre_labels,
                "platforms": [
                    {"platform": r.platform, "type": r.access_type}
                    for r in avail_map.get(m.id, [])[:3]
                ],
            })

        app.logger.info("Discover queue length for user %s: %d", user.id, len(queue))

        return render_template(
            "discover.html",
            queue=queue,
            queue_empty=(len(queue) == 0),
            min_rating=min_rating,
            error=error,
        )

    @app.route("/discover/swipe", methods=["POST"])
    @login_required
    def discover_swipe():
        user = current_user()
        body = request.get_json(silent=True) or {}
        tmdb_id = body.get("tmdb_id")
        direction = body.get("direction")

        if not tmdb_id or direction not in ("left", "right"):
            return jsonify({"error": "bad request"}), 400

        if direction == "right":
            movie = _ensure_movie(int(tmdb_id))
            existing = UserMovieStatus.query.filter_by(
                user_id=user.id, movie_id=movie.id
            ).one_or_none()
            if existing is None:
                db.session.add(UserMovieStatus(
                    user_id=user.id, movie_id=movie.id, status="want_to_watch"
                ))
                db.session.commit()
        else:
            # Left swipe: track in session so this movie doesn't reappear this visit.
            dismissed = session.get("discover_dismissed", [])
            if tmdb_id not in dismissed:
                dismissed.append(tmdb_id)
            session["discover_dismissed"] = dismissed

        return jsonify({"ok": True})

    # --- Analytics ---------------------------------------------------------

    @app.route("/analytics")
    @login_required
    def analytics():
        user = current_user()

        status_rows = UserMovieStatus.query.filter_by(user_id=user.id).all()
        status_counts = {s: 0 for s in VALID_STATUSES}
        seen_movie_ids = []
        for row in status_rows:
            status_counts[row.status] = status_counts.get(row.status, 0) + 1
            if row.status == "seen":
                seen_movie_ids.append(row.movie_id)

        seen_movies: list[Movie] = []
        if seen_movie_ids:
            seen_movies = Movie.query.filter(Movie.id.in_(seen_movie_ids)).all()

        gmap = genre_map()
        genre_counter: Counter[str] = Counter()
        for m in seen_movies:
            for gid in m.genre_ids():
                name = gmap.get(gid)
                if name:
                    genre_counter[name] += 1
        top_genre_labels = [g for g, _ in genre_counter.most_common(8)]
        top_genre_values = [genre_counter[g] for g in top_genre_labels]

        year_counter: Counter[int] = Counter()
        for m in seen_movies:
            if m.release_year:
                year_counter[m.release_year] += 1
        year_labels = sorted(year_counter)
        year_values = [year_counter[y] for y in year_labels]

        ratings = [r.rating for r in UserRating.query.filter_by(user_id=user.id).all()]
        avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None

        data = {
            "statusLabels": ["Seen", "Not seen", "Want to watch"],
            "statusValues": [
                status_counts.get("seen", 0),
                status_counts.get("not_seen", 0),
                status_counts.get("want_to_watch", 0),
            ],
            "genreLabels": top_genre_labels,
            "genreValues": top_genre_values,
            "yearLabels": [str(y) for y in year_labels],
            "yearValues": year_values,
        }

        return render_template(
            "analytics.html",
            chart_data=data,
            avg_rating=avg_rating,
            rating_count=len(ratings),
            total_seen=status_counts.get("seen", 0),
        )

    # --- Auth --------------------------------------------------------------

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            if not username or not email or len(password) < 6:
                flash("Username, email, and a 6+ char password are required.", "danger")
                return render_template("register.html")
            if User.query.filter((User.username == username) | (User.email == email)).first():
                flash("Username or email already taken.", "danger")
                return render_template("register.html")
            user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
            )
            db.session.add(user)
            db.session.commit()
            session["user_id"] = user.id
            flash("Welcome aboard!", "success")
            return redirect(url_for("index"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            identifier = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            user = User.query.filter(
                (User.username == identifier) | (User.email == identifier.lower())
            ).first()
            if user and check_password_hash(user.password_hash, password):
                session["user_id"] = user.id
                flash("Logged in.", "success")
                nxt = request.args.get("next") or url_for("index")
                return redirect(nxt)
            flash("Invalid credentials.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.pop("user_id", None)
        session.pop("discover_dismissed", None)
        flash("Signed out.", "info")
        return redirect(url_for("index"))

    # --- helpers -----------------------------------------------------------

    def _form_or_json(key: str):
        if request.is_json:
            body = request.get_json(silent=True) or {}
            return body.get(key)
        return request.form.get(key)

    def _ensure_movie(tmdb_id: int) -> Movie:
        movie = Movie.query.filter_by(tmdb_id=tmdb_id).one_or_none()
        if movie is None:
            try:
                payload = movie_detail(tmdb_id)
            except TMDbError:
                abort(404)
            movie = upsert_movie_from_tmdb(payload)
            db.session.commit()
        return movie

    def _json_or_redirect(body: dict, tmdb_id: int, status_code: int = 200):
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify(body), status_code
        if status_code >= 400:
            flash(body.get("error", "Something went wrong."), "danger")
        return redirect(url_for("movie_page", tmdb_id=tmdb_id))


# ---------------------------------------------------------------------------
# Availability helpers
# ---------------------------------------------------------------------------


def _has_free(avail_rows: list[Availability]) -> bool:
    """True if any availability row is subscription or free (included/no-cost)."""
    return any(r.access_type in ("subscription", "free") for r in avail_rows)


def _matches_platforms(avail_rows: list[Availability], selected: list[str]) -> bool:
    """True if at least one cached availability row is on a wanted platform.

    selected is a list of slugs e.g. ["netflix", "prime"]. Both the stored
    platform (a display name like "prime video") and the selected slug are run
    through canonical_platform() so e.g. "prime video" matches the "prime" slug.
    Returns True unconditionally when selected is empty.
    """
    if not selected:
        return True
    wanted = {canonical_platform(p) for p in selected}
    return any(canonical_platform(r.platform) in wanted for r in avail_rows)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _top_genres_for_user(user_id: int, limit: int = 3) -> list[int]:
    seen = (db.session.query(Movie.genres)
            .join(UserMovieStatus, UserMovieStatus.movie_id == Movie.id)
            .filter(UserMovieStatus.user_id == user_id,
                    UserMovieStatus.status == "seen")
            .all())
    counter: Counter[int] = Counter()
    for (genres_json,) in seen:
        if not genres_json:
            continue
        try:
            for gid in json.loads(genres_json):
                counter[int(gid)] += 1
        except (ValueError, TypeError):
            continue
    return [gid for gid, _ in counter.most_common(limit)]


def _score_movie(movie: Movie, top_genres: list[int], this_year: int) -> float:
    base = (movie.tmdb_rating or 0.0) * 0.6

    recency = 0.0
    if movie.release_year:
        age = this_year - movie.release_year
        if age <= 1:
            recency = 2.0
        elif age <= 2:
            recency = 1.0

    genre_match = 0.0
    if top_genres:
        movie_genres = set(movie.genre_ids())
        genre_match = float(sum(1 for g in top_genres if g in movie_genres))

    return round(base + recency + genre_match, 3)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _register_template_helpers(app: Flask) -> None:

    @app.template_filter("poster")
    def _poster(path: str | None, size: str = "w342") -> str:
        return poster_url(path, size) or url_for("static", filename="poster-placeholder.svg")

    @app.template_filter("backdrop")
    def _backdrop(path: str | None, size: str = "w1280") -> str | None:
        return backdrop_url(path, size)


def _register_context(app: Flask) -> None:

    @app.context_processor
    def inject_user():
        return {"current_user": current_user()}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
