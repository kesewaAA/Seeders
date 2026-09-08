# Movie Tracker

A Flask + SQLite movie tracker powered by the TMDb API. Search films, mark them as *Seen*, *Not Seen*, or *Want to Watch*, rate them, get personalised recommendations, and explore a small analytics dashboard.

## Stack

- Python 3.10+ / Flask
- SQLite via Flask-SQLAlchemy
- TMDb v3 API (`requests`)
- Jinja2 templates + Bootstrap 5 (CDN) + Chart.js (CDN)

## Setup

1. **Get a TMDb API key**  
   Create a free account at [themoviedb.org](https://www.themoviedb.org/), go to *Settings → API*, and copy your **API Read Access** v3 key.

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

   Then edit `.env` and fill in `TMDB_API_KEY` and a random `SECRET_KEY`.

4. **Seed the database** (optional but recommended — gives the home page and recommendations some content out of the box)

   ```bash
   python seed.py
   ```

5. **Run the app**

   ```bash
   flask --app app run --debug
   ```

   Open <http://127.0.0.1:5000/>.

## Features

- **Search** hits TMDb `/search/movie` and caches results locally.
- **Status tracking** — one of `seen`, `not_seen`, `want_to_watch` per user per movie.
- **Ratings** — 1 to 10, one per user per movie.
- **Recommendations** — `(tmdb_rating * 0.6) + recency_bonus + genre_match_bonus`, excluding movies you've already marked as *seen*. Scores are recomputed on every request.
- **Filters** — genres (multi), release-year range, minimum TMDb rating.
- **Analytics** — most-watched genre, average rating given, status breakdown, and movies watched per year (Chart.js).

## Notes

- "Not seen" in analytics means an **explicit** `not_seen` status. Movies you never interacted with aren't counted anywhere.
- Genres are stored on each movie as a JSON list of TMDb genre IDs; names come from TMDb's genre list endpoint (cached in memory).
- All DB writes use parameterised SQL via SQLAlchemy.
