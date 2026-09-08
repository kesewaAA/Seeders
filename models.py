"""SQLAlchemy models for the Seeders app."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


VALID_STATUSES = ("seen", "not_seen", "want_to_watch")


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    statuses = db.relationship("UserMovieStatus", backref="user", lazy="dynamic",
                               cascade="all, delete-orphan")
    ratings = db.relationship("UserRating", backref="user", lazy="dynamic",
                              cascade="all, delete-orphan")
    preference = db.relationship("UserPreference", backref="user", uselist=False,
                                 cascade="all, delete-orphan")


class Movie(db.Model):
    __tablename__ = "movies"

    id = db.Column(db.Integer, primary_key=True)
    tmdb_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    release_year = db.Column(db.Integer, nullable=True)
    # JSON-encoded list of TMDb genre ids, e.g. "[28, 12]"
    genres = db.Column(db.Text, nullable=True)
    poster_path = db.Column(db.String(255), nullable=True)
    backdrop_path = db.Column(db.String(255), nullable=True)
    tmdb_rating = db.Column(db.Float, nullable=True)
    overview = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    statuses = db.relationship("UserMovieStatus", backref="movie", lazy="dynamic",
                               cascade="all, delete-orphan")
    ratings = db.relationship("UserRating", backref="movie", lazy="dynamic",
                              cascade="all, delete-orphan")
    availability = db.relationship("Availability", backref="movie", lazy="dynamic",
                                   cascade="all, delete-orphan")

    def genre_ids(self) -> list[int]:
        if not self.genres:
            return []
        try:
            data = json.loads(self.genres)
            return [int(g) for g in data]
        except (ValueError, TypeError):
            return []

    def set_genre_ids(self, ids: list[int]) -> None:
        self.genres = json.dumps(list(ids))


class UserMovieStatus(db.Model):
    __tablename__ = "user_movie_status"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.id", ondelete="CASCADE"),
                         nullable=False)
    status = db.Column(db.String(20), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "movie_id", name="uq_status_user_movie"),
    )


class UserRating(db.Model):
    __tablename__ = "user_ratings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.id", ondelete="CASCADE"),
                         nullable=False)
    rating = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "movie_id", name="uq_rating_user_movie"),
    )


def upsert_movie_from_tmdb(payload: dict) -> Movie | None:
    """Insert or update a Movie from a TMDb payload. Commits nothing; caller does."""
    tmdb_id = payload.get("id")
    if not tmdb_id:
        return None

    movie = Movie.query.filter_by(tmdb_id=tmdb_id).one_or_none()
    if movie is None:
        movie = Movie(tmdb_id=tmdb_id, title=payload.get("title") or "Untitled")
        db.session.add(movie)

    movie.title = payload.get("title") or movie.title or "Untitled"
    release_date = payload.get("release_date") or ""
    if release_date and len(release_date) >= 4 and release_date[:4].isdigit():
        movie.release_year = int(release_date[:4])

    # TMDb returns `genre_ids` on list endpoints and `genres` (objects) on detail endpoints.
    if "genre_ids" in payload and payload["genre_ids"] is not None:
        movie.set_genre_ids(payload["genre_ids"])
    elif "genres" in payload and payload["genres"] is not None:
        movie.set_genre_ids([g["id"] for g in payload["genres"] if "id" in g])

    movie.poster_path = payload.get("poster_path") or movie.poster_path
    movie.backdrop_path = payload.get("backdrop_path") or movie.backdrop_path
    rating = payload.get("vote_average")
    if rating is not None:
        movie.tmdb_rating = float(rating)
    movie.overview = payload.get("overview") or movie.overview

    return movie


# ---------------------------------------------------------------------------
# Streaming availability
# ---------------------------------------------------------------------------

STALE_AFTER_HOURS = 24


class Availability(db.Model):
    __tablename__ = "availability"

    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.id", ondelete="CASCADE"),
                         nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    access_type = db.Column(db.String(20), nullable=False)  # subscription/free/addon/rent/buy
    link = db.Column(db.String(500), nullable=True)
    price = db.Column(db.Float, nullable=True)
    quality = db.Column(db.String(20), nullable=True)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("movie_id", "platform", "access_type",
                            name="uq_availability_movie_platform_type"),
    )


def upsert_availability(movie_id: int, options: list[dict]) -> None:
    """Replace all cached availability rows for a movie. Commits nothing; caller does.

    Availability changes frequently, so we delete and re-insert rather than
    trying to diff, which would leave stale entries when platforms are removed.

    The API often returns several options that share the same
    (platform, access_type) but differ by quality/price (e.g. Prime "buy" in
    both HD and UHD). The table has a UNIQUE(movie_id, platform, access_type)
    constraint, so we must collapse those to one row per key BEFORE inserting.
    Strategy: cheapest-wins — keep the lowest-priced offer for each key, since
    that's what a user cares about ("can I buy it and roughly how much").
    De-duplicating in a dict up front means duplicates overwrite in memory and
    no duplicate ever reaches the DB.
    """
    Availability.query.filter_by(movie_id=movie_id).delete(synchronize_session=False)
    now = datetime.utcnow()

    deduped: dict[tuple[str, str], dict] = {}
    for opt in options:
        platform = opt.get("platform") or "unknown"
        access_type = opt.get("type") or "subscription"
        key = (platform, access_type)

        existing = deduped.get(key)
        if existing is None:
            deduped[key] = opt
            continue

        # Keep the cheapest offer. A missing price is treated as "unknown" and
        # only used when no priced alternative exists.
        new_price = opt.get("price")
        old_price = existing.get("price")
        if new_price is not None and (old_price is None or new_price < old_price):
            deduped[key] = opt

    for (platform, access_type), opt in deduped.items():
        db.session.add(Availability(
            movie_id=movie_id,
            platform=platform,
            access_type=access_type,
            link=opt.get("link") or None,
            price=opt.get("price"),
            quality=opt.get("quality"),
            fetched_at=now,
        ))


def is_availability_stale(movie: Movie) -> bool:
    """Return True if availability data is absent or older than STALE_AFTER_HOURS."""
    newest = (Availability.query
              .filter_by(movie_id=movie.id)
              .order_by(Availability.fetched_at.desc())
              .first())
    if newest is None:
        return True
    cutoff = datetime.utcnow() - timedelta(hours=STALE_AFTER_HOURS)
    return newest.fetched_at < cutoff


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------


class UserPreference(db.Model):
    __tablename__ = "user_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
                        unique=True, nullable=False)
    min_rating = db.Column(db.Float, default=0.0, nullable=False)
    free_only = db.Column(db.Boolean, default=False, nullable=False)
    # JSON list of platform slugs, same pattern as Movie.genres
    preferred_platforms = db.Column(db.Text, nullable=True)

    def platform_list(self) -> list[str]:
        if not self.preferred_platforms:
            return []
        try:
            return json.loads(self.preferred_platforms)
        except (ValueError, TypeError):
            return []


def get_or_create_preference(user_id: int) -> UserPreference:
    """Return the user's preference row, creating a default one if absent.

    Does not commit — caller is responsible for committing when needed.
    """
    pref = UserPreference.query.filter_by(user_id=user_id).one_or_none()
    if pref is None:
        pref = UserPreference(user_id=user_id, min_rating=0.0, free_only=False)
        db.session.add(pref)
    return pref
