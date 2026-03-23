# restaurant_client.py
# HTTP client used by api.py to talk to the restaurant service.
#
# Reads RESTAURANT_API_URL from env (default: http://localhost:6333).
# To switch to the hosted service, set:
#   RESTAURANT_API_URL=https://restaurant.tomenu.sk
#   RESTAURANT_API_KEY=your-key   (only needed for the hosted service)
#
# The main backend (api.py) should NEVER import from scrapers/db.py
# for restaurant data — always go through this client.

import os
from typing import Optional

import httpx

RESTAURANT_API_URL = os.environ.get("RESTAURANT_API_URL", "http://localhost:6333").rstrip("/")
RESTAURANT_API_KEY = os.environ.get("RESTAURANT_API_KEY", "")

# Shared httpx client — reused across requests for connection pooling
_client = httpx.Client(
    base_url=RESTAURANT_API_URL,
    headers={"Authorization": RESTAURANT_API_KEY} if RESTAURANT_API_KEY else {},
    timeout=5.0,
)


class RestaurantServiceError(Exception):
    """Raised when the restaurant service returns an error or is unreachable."""
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"restaurant service {status}: {detail}")


def _get(path: str, params: dict = None) -> dict | list:
    try:
        r = _client.get(path, params=params)
    except httpx.ConnectError:
        raise RestaurantServiceError(503, "restaurant service unreachable")
    except httpx.TimeoutException:
        raise RestaurantServiceError(504, "restaurant service timed out")

    if r.status_code == 404:
        raise RestaurantServiceError(404, r.json().get("detail", "not found"))
    if not r.is_success:
        detail = r.json().get("detail", r.text) if r.headers.get("content-type","").startswith("application/json") else r.text
        raise RestaurantServiceError(r.status_code, detail)

    return r.json()


# ── public interface ──────────────────────────────────────────────────────────

def get_cities() -> list[dict]:
    """All cities with restaurant counts and coordinates."""
    return _get("/cities")


def get_city(city_slug: str) -> dict:
    """Single city profile."""
    return _get(f"/cities/{city_slug}")


def get_restaurants(
    city_slug: str,
    delivery: Optional[bool] = None,
    verified: Optional[bool] = None,
) -> list[dict]:
    """
    All restaurants for a city.
    Returns the 'restaurants' list directly (not the wrapper dict).
    """
    params = {}
    if delivery is not None: params["delivery"] = str(delivery).lower()
    if verified is not None: params["verified"] = str(verified).lower()
    data = _get(f"/restaurants/{city_slug}", params=params)
    return data.get("restaurants", [])


def get_restaurant(city_slug: str, slug: str) -> dict:
    """Single restaurant profile."""
    return _get(f"/restaurants/{city_slug}/{slug}")


def build_restaurant_map(city_slug: str) -> dict[str, dict]:
    """
    Returns a slug→profile dict for fast lookups when joining with menu data.
    Call once per request, not per dish.
    """
    rests = get_restaurants(city_slug)
    return {r["slug"]: r for r in rests}