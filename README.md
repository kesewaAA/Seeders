# Seeders

A Flask + SQLite movie discovery app powered by TMDb and the Movie of the Night streaming availability API. Search films, track what you've seen, rate them, get personalised recommendations, swipe through a Tinder-style discovery queue, and see it all filtered by where you can actually watch.

## Stack

- Python 3.10+ / Flask
- SQLite via Flask-SQLAlchemy
- TMDb v3 API (`requests`) for movie data
- Movie of the Night direct API (`developers.movieofthenight.com`) for streaming availability
- Jinja2 templates + Bootstrap 5 (CDN) + Chart.js (CDN)
- Password hashing via Werkzeug, session-based auth

## Setup

1. **Get API keys**

   - TMDb: create a free account at [themoviedb.org](https://www.themoviedb.org/), go to *Settings → API*, and copy your v3 API key.
   - Movie of the Night: sign up at [developers.movieofthenight.com](https://developers.movieofthenight.com/) for a streaming availability key. This is optional — the app runs fine without it, it just won't have platform/pricing data to filter or show.

2. **Install dependencies**

   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS / Linux
   source .venv/bin/activate

   pip install -r requirements.txt
   ```

3. **Configure environment**

   ```bash
   cp .env.example .env
   ```

   Then edit `.env` and fill in `TMDB_API_KEY`, a random `SECRET_KEY`, and (optionally) `MOTN_API_KEY`.

4. **Seed the database** (optional but recommended — gives the home page and recommendations some content out of the box)

   ```bash
   python seed.py
   ```

5. **Populate streaming availability** (optional, needs `MOTN_API_KEY`)

   ```bash
   python fetch_availability.py        # default 50 movies
   python fetch_availability.py 100    # up to the daily free-tier limit
   ```

   Prioritises movies with no cached availability yet, so re-running is safe. Until this has been run at least once, availability-based filters (free-only, platform) are skipped rather than hiding the whole catalog.

6. **Run the app**

   ```bash
   flask --app app run --debug
   ```

   Open <http://127.0.0.1:5000/>.

## Features

- **Accounts** — register/login with hashed passwords and session-based auth. Most features (status, ratings, watchlist, recommendations, discover, analytics, preferences) require login.
- **Search** — hits TMDb `/search/movie`, caches results locally, and filters by minimum rating, free-only, and streaming platform.
- **Movie detail page** — TMDb metadata plus cached streaming availability, grouped by access type (subscription, free, rent, buy). Availability is refreshed automatically when stale and fails silently (never 500s) if the streaming API errors out.
- **Status tracking** — mark a movie `seen`, `not_seen`, or `want_to_watch`, one status per user per movie.
- **Ratings** — 1 to 10, one per user per movie.
- **Watchlist** — everything marked `want_to_watch`, most recently updated first.
- **Recommendations** — scored as `(tmdb_rating * 0.6) + recency_bonus + genre_match_bonus`, using your most-watched genres and excluding movies already marked `seen`. Filterable by genre, release-year range, minimum rating, free-only, and platform.
- **Discover (swipe mode)** — a Tinder-style queue of unrated movies pulled from TMDb's trending/recent-popular feed plus the local cache. Swipe right adds to the watchlist; swipe left dismisses for the session.
- **Saved preferences** — minimum rating, free-only, and preferred platforms persist per user and pre-fill search, recommendations, and discover.
- **Analytics** — status breakdown, top genres among seen movies, movies watched per year, and average rating given, rendered with Chart.js.

## Notes

- "Not seen" in analytics means an **explicit** `not_seen` status — movies you never interacted with aren't counted anywhere.
- Genres are stored on each movie as a JSON list of TMDb genre IDs; names come from TMDb's genre list endpoint (cached in memory).
- Streaming platform names are normalized through a canonical slug map (e.g. "Prime Video", "Amazon Video" → `prime`) so saved preferences, filters, and cached availability all compare equal regardless of how the source labeled the platform.
- `db.create_all()` picks up new tables on the next run with no migration tool. For schema changes to *existing* tables, you'd need to add Alembic — not currently set up.
- All DB writes use parameterised SQL via SQLAlchemy.

### Credits
- Made with Claude AI
