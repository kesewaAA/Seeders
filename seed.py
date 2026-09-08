"""Seed the movies table with 20 popular movies from TMDb.

Run once after setting up your .env:
    python seed.py
"""
from __future__ import annotations

from app import app
from models import Movie, db, upsert_movie_from_tmdb
from tmdb import TMDbError, discover_recent_popular, trending_week


def seed(target: int = 20) -> None:
    with app.app_context():
        db.create_all()
        payloads: list[dict] = []
        seen_ids: set[int] = set()

        try:
            for payload in trending_week():
                if payload.get("id") in seen_ids:
                    continue
                payloads.append(payload)
                seen_ids.add(payload["id"])
                if len(payloads) >= target:
                    break

            if len(payloads) < target:
                for payload in discover_recent_popular():
                    if payload.get("id") in seen_ids:
                        continue
                    payloads.append(payload)
                    seen_ids.add(payload["id"])
                    if len(payloads) >= target:
                        break
        except TMDbError as exc:
            print(f"TMDb error during seed: {exc}")
            return

        for payload in payloads[:target]:
            upsert_movie_from_tmdb(payload)
        db.session.commit()

        total = Movie.query.count()
        print(f"Seeded {len(payloads[:target])} movies. movies table now has {total} rows.")


if __name__ == "__main__":
    seed()
