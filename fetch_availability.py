"""Bulk-populate the availability cache from the Movie of the Night API.

Usage:
    python fetch_availability.py          # default 50 movies
    python fetch_availability.py 30       # limit to 30 API calls
    python fetch_availability.py 100      # up to the daily free-tier limit

Prioritises movies with no existing availability rows so re-running is safe.
Sleeps 1 s between calls to stay comfortably under the API's rate limits.
One failure never aborts the whole run.
"""
from __future__ import annotations

import sys
import time

from dotenv import load_dotenv

load_dotenv()

from app import app
from models import Availability, Movie, db, upsert_availability
from streaming import StreamingError, get_availability_by_tmdb_id


def run(limit: int = 50) -> None:
    with app.app_context():
        # Prioritise movies that have no availability rows yet.
        have_avail = {row.movie_id for row in
                      db.session.query(Availability.movie_id).distinct().all()}
        all_movies = Movie.query.order_by(Movie.tmdb_rating.desc().nullslast()).all()

        # Put un-cached movies first, then already-cached ones (for refresh).
        queue = [m for m in all_movies if m.id not in have_avail]
        queue += [m for m in all_movies if m.id in have_avail]

        total = min(limit, len(queue))
        done = 0
        print(f"Fetching availability for up to {total} movies …\n")

        for m in queue:
            if done >= limit:
                break
            try:
                options = get_availability_by_tmdb_id(m.tmdb_id)
                upsert_availability(m.id, options)
                db.session.commit()
                done += 1
                print(f"  {done:>3}/{total}  {m.title} — {len(options)} option(s)")
            except StreamingError as exc:
                db.session.rollback()
                print(f"  SKIP  {m.title} (tmdb {m.tmdb_id}): {exc}")
            except Exception as exc:
                db.session.rollback()
                print(f"  ERROR {m.title} (tmdb {m.tmdb_id}): {exc}")

            if done < limit:
                time.sleep(1)

        print(f"\nDone. {done} movies updated.")
        total_rows = Availability.query.count()
        print(f"availability table now has {total_rows} row(s).")


if __name__ == "__main__":
    limit = 50
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            print(f"Usage: python fetch_availability.py [limit]")
            sys.exit(1)
    run(limit)
