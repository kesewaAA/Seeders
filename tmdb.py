"""TMDb API client. All outbound calls live here."""
from __future__ import annotations

import os
import warnings
from datetime import date, timedelta
from typing import Any

import requests
import urllib3
from requests.adapters import HTTPAdapter

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"
DEFAULT_TIMEOUT = 10

_genre_cache: dict[int, str] | None = None


class TMDbError(RuntimeError):
    """Raised when a TMDb request fails."""


# ---------------------------------------------------------------------------
# Shared session — verify=False works around Windows SSL inspection software
# (Defender, antivirus, corporate proxies) that causes UNEXPECTED_EOF errors.
# This is safe for a local dev app; never disable verification in production.
# ---------------------------------------------------------------------------

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _make_session() -> requests.Session:
    retry = urllib3.Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods={"GET"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.verify = False  # bypass SSL certificate check — dev only
    return s


_session: requests.Session = _make_session()


def _api_key() -> str:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        raise TMDbError("TMDB_API_KEY is not set. Copy .env.example to .env and add your key.")
    return key


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    params = dict(params or {})
    params["api_key"] = _api_key()
    url = f"{BASE_URL}{path}"
    try:
        resp = _session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise TMDbError(f"Network error talking to TMDb: {exc}") from exc
    if resp.status_code != 200:
        raise TMDbError(f"TMDb {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def poster_url(path: str | None, size: str = "w342") -> str | None:
    if not path:
        return None
    return f"{IMAGE_BASE}/{size}{path}"


def backdrop_url(path: str | None, size: str = "w1280") -> str | None:
    if not path:
        return None
    return f"{IMAGE_BASE}/{size}{path}"


def search_movies(query: str, page: int = 1) -> list[dict]:
    if not query.strip():
        return []
    data = _get("/search/movie", {"query": query, "page": page, "include_adult": "false"})
    return data.get("results", [])


def movie_detail(tmdb_id: int) -> dict:
    return _get(f"/movie/{tmdb_id}")


def trending_week(page: int = 1) -> list[dict]:
    data = _get("/trending/movie/week", {"page": page})
    return data.get("results", [])


def discover_recent_popular(page: int = 1, months_back: int = 12) -> list[dict]:
    start = (date.today() - timedelta(days=30 * months_back)).isoformat()
    data = _get("/discover/movie", {
        "sort_by": "popularity.desc",
        "primary_release_date.gte": start,
        "include_adult": "false",
        "page": page,
    })
    return data.get("results", [])


def discover(
    *,
    page: int = 1,
    with_genres: list[int] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_rating: float | None = None,
    sort_by: str = "popularity.desc",
) -> list[dict]:
    params: dict[str, Any] = {
        "sort_by": sort_by,
        "include_adult": "false",
        "page": page,
    }
    if with_genres:
        params["with_genres"] = ",".join(str(g) for g in with_genres)
    if year_min is not None:
        params["primary_release_date.gte"] = f"{year_min}-01-01"
    if year_max is not None:
        params["primary_release_date.lte"] = f"{year_max}-12-31"
    if min_rating is not None:
        params["vote_average.gte"] = min_rating
    data = _get("/discover/movie", params)
    return data.get("results", [])


def genre_map() -> dict[int, str]:
    """Return a cached {genre_id: name} map from /genre/movie/list."""
    global _genre_cache
    if _genre_cache is not None:
        return _genre_cache
    try:
        data = _get("/genre/movie/list", {"language": "en-US"})
    except TMDbError:
        _genre_cache = {}
        return _genre_cache
    _genre_cache = {g["id"]: g["name"] for g in data.get("genres", [])}
    return _genre_cache
