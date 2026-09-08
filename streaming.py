"""Streaming Availability API client (Movie of the Night — direct developer API).

Mirrors the structure of tmdb.py — plain functions, no classes, custom error.
Uses the direct API at developers.movieofthenight.com (NOT RapidAPI).
"""
from __future__ import annotations

import os
from typing import Any

import requests
import urllib3
from requests.adapters import HTTPAdapter

BASE_URL = "https://api.movieofthenight.com/v4"
DEFAULT_TIMEOUT = 10

# Canonical platform slugs shared by the filter UI, saved preferences, and
# availability matching. The API stores platforms by display name (e.g.
# "Prime Video"), but the filter checkboxes use short slugs ("prime"). This
# maps every known display-name / id / alias to one canonical slug so both
# sides compare equal regardless of which form was stored.
_PLATFORM_ALIASES: dict[str, str] = {
    "netflix": "netflix",
    "prime": "prime",
    "prime video": "prime",
    "amazon prime video": "prime",
    "amazon video": "prime",
    "amazon": "prime",
    "hulu": "hulu",
    "max": "max",
    "hbo max": "max",
    "hbo": "max",
    "disney": "disney",
    "disney+": "disney",
    "disney plus": "disney",
    "apple": "apple",
    "apple tv": "apple",
    "apple tv+": "apple",
    "apple tv plus": "apple",
    "itunes": "apple",
    "tubi": "tubi",
    "paramount": "paramount",
    "paramount+": "paramount",
    "paramount plus": "paramount",
    "peacock": "peacock",
    "starz": "starz",
    "showtime": "showtime",
}


def canonical_platform(value: str | None) -> str:
    """Normalise any platform name / id / alias to a canonical slug.

    Unknown values fall back to their lowercased, stripped form so matching
    still works for platforms not in the alias table.
    """
    if not value:
        return ""
    key = value.strip().lower()
    return _PLATFORM_ALIASES.get(key, key)


# Maps API service type values to our normalised access_type vocabulary.
_TYPE_MAP: dict[str, str] = {
    "subscription": "subscription",
    "free": "free",
    "addon": "addon",
    "rent": "rent",
    "buy": "buy",
    "tvodrent": "rent",
    "tvodbuy": "buy",
}


class StreamingError(RuntimeError):
    """Raised when a Streaming Availability API request fails."""


# Shared session — same verify=False approach as tmdb.py (Windows SSL fix).
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
    s.verify = False
    return s


_session: requests.Session = _make_session()


def _api_key() -> str:
    key = os.environ.get("MOTN_API_KEY", "").strip()
    if not key:
        raise StreamingError(
            "MOTN_API_KEY is not set. Copy .env.example to .env, add your key "
            "from developers.movieofthenight.com (Movie of the Night direct API)."
        )
    return key


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    headers = {"X-API-Key": _api_key()}
    url = f"{BASE_URL}{path}"

    # Temporary debug output — confirm the direct API is reached and returns 200.
    print(f"[STREAMING] GET {url} params={params or {}}")

    try:
        resp = _session.get(url, headers=headers, params=params or {},
                            timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise StreamingError(f"Network error talking to Streaming API: {exc}") from exc

    print(f"[STREAMING] status={resp.status_code} body={resp.text[:500]}")

    if resp.status_code == 429:
        raise StreamingError("Streaming API rate limit hit. Try again shortly.")
    if resp.status_code != 200:
        raise StreamingError(f"Streaming API {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _normalise_option(option: dict) -> dict:
    """Normalise one raw streaming option entry into our internal shape.

    API response shape (v4):
      {
        "service": {"id": "netflix", "name": "Netflix", ...},
        "type": "subscription" | "free" | "addon" | "rent" | "buy",
        "link": "https://...",
        "quality": "hd" | "4k" | ...,   # optional
        "price": {"amount": "3.99", "currency": "USD", ...}  # rent/buy only
      }
    """
    raw_type = (option.get("type") or "").lower()
    access_type = _TYPE_MAP.get(raw_type, raw_type) or "subscription"

    # service is a nested object; fall back through likely key names
    service = option.get("service") or {}
    if isinstance(service, dict):
        platform = (service.get("name") or service.get("id") or "unknown").lower()
    else:
        platform = str(service).lower()

    price: float | None = None
    price_raw = option.get("price")
    if isinstance(price_raw, dict):
        try:
            price = float(price_raw.get("amount", 0) or 0)
        except (TypeError, ValueError):
            price = None
    elif price_raw is not None:
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = None

    # quality is a top-level string on the option object in v4
    quality: str | None = None
    for q_key in ("quality", "videoQuality"):
        val = option.get(q_key)
        if val:
            quality = str(val).upper()
            break

    return {
        "platform": platform,
        "type": access_type,
        "link": option.get("link") or option.get("videoLink") or option.get("url") or "",
        "price": price,
        "quality": quality,
    }


def get_availability_by_tmdb_id(tmdb_id: int, country: str = "us") -> list[dict]:
    """Return normalised streaming options for a movie.

    Endpoint: GET /shows/movie/{tmdb_id}?country={country}

    Returns [] when the title has no streaming options in that country.
    Raises StreamingError on network or API errors so the caller can decide
    whether to surface or suppress the failure.
    """
    data = _get(f"/shows/movie/{tmdb_id}", {"country": country})

    # Response is the show object directly (not wrapped in a results list).
    streaming_options = data.get("streamingOptions") or {}
    country_options = streaming_options.get(country) or []

    return [_normalise_option(opt) for opt in country_options]
