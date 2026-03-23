# restaurant_api.py
# Local restaurant data API — stand-in for the future restaurant.tomenu.sk service.
#
# Exposes the exact same contract that restaurant.tomenu.sk will have.
# When you are ready to switch, set RESTAURANT_API_URL=https://restaurant.tomenu.sk
# in your environment and stop running this file. Nothing else changes.
#
# Reads from namenu.db (the scraper database).
# Does NOT know about menus, dishes, tags, users, or auth.
#
# Run standalone:
#   uvicorn restaurant_api:app --port 6333 --reload
#
# Default port: 6333 (override with RESTAURANT_API_PORT env var)

import json
import os
import sqlite3
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

NAMENU_DB = os.environ.get("NAMENU_DB", "namenu.db")

app = FastAPI(
    title="ToMenu Restaurant API",
    description="Restaurant profiles and city data. Local stand-in for restaurant.tomenu.sk.",
    version="0.1.0",
)

# Allow the main backend (api.py) to call this freely when both run locally
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "PATCH"],
    allow_headers=["*"],
)


# ── db ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(NAMENU_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


# ── serialization ─────────────────────────────────────────────────────────────

def _rest_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["active_days"] = json.loads(d.get("active_days") or "[]")
    except Exception:
        d["active_days"] = []
    return d


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "service": "restaurant-api"}


@app.get("/cities", summary="List all cities")
def list_cities(db: sqlite3.Connection = Depends(get_db)):
    """
    Returns all cities that have at least one restaurant.
    Shape is stable — restaurant.tomenu.sk will return the same fields.
    """
    rows = db.execute(
        "SELECT id, name, slug, url, lat, lon, restaurant_count FROM cities ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/cities/{city_slug}", summary="Get a single city")
def get_city(city_slug: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute(
        "SELECT id, name, slug, url, lat, lon, restaurant_count FROM cities WHERE slug = ?",
        (city_slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"City '{city_slug}' not found")
    return dict(row)


@app.get("/restaurants/{city_slug}", summary="List restaurants for a city")
def list_restaurants(
    city_slug: str,
    delivery: Optional[bool] = Query(default=None),
    verified: Optional[bool] = Query(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Returns all restaurants for a city with their profile data.
    Does NOT include menu items — those live in the main backend.
    """
    q = """
        SELECT r.id, r.slug, r.name, r.address, r.phone,
               r.delivery, r.info, r.description, r.profile_picture,
               r.verified, r.active_days, r.last_seen,
               c.slug AS city_slug, c.name AS city_name
        FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ?
    """
    params: list = [city_slug]

    if delivery is not None:
        q += " AND r.delivery = ?"
        params.append(1 if delivery else 0)
    if verified is not None:
        q += " AND r.verified = ?"
        params.append(1 if verified else 0)

    q += " ORDER BY r.name"
    rows = db.execute(q, params).fetchall()

    if not rows:
        # Check if the city exists at all
        city = db.execute("SELECT id FROM cities WHERE slug = ?", (city_slug,)).fetchone()
        if not city:
            raise HTTPException(status_code=404, detail=f"City '{city_slug}' not found")

    return {
        "city_slug": city_slug,
        "count":     len(rows),
        "restaurants": [_rest_to_dict(r) for r in rows],
    }


@app.get("/restaurants/{city_slug}/{slug}", summary="Get a single restaurant profile")
def get_restaurant(
    city_slug: str,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute("""
        SELECT r.id, r.slug, r.name, r.address, r.phone,
               r.delivery, r.info, r.description, r.profile_picture,
               r.verified, r.active_days, r.last_seen,
               c.slug AS city_slug, c.name AS city_name
        FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ? AND r.slug = ?
    """, (city_slug, slug)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    return _rest_to_dict(row)


# ── write endpoints ───────────────────────────────────────────────────────────
# These exist locally so you can edit restaurant metadata without a full
# Restaurant DB. restaurant.tomenu.sk will have its own write endpoints
# with proper auth — these are admin-only on localhost.

class RestaurantUpdate(BaseModel):
    description:     Optional[str] = None
    profile_picture: Optional[str] = None
    verified:        Optional[int] = None
    info:            Optional[str] = None
    phone:           Optional[str] = None
    address:         Optional[str] = None


@app.patch("/restaurants/{city_slug}/{slug}", summary="Update restaurant profile fields")
def update_restaurant(
    city_slug: str,
    slug: str,
    body: RestaurantUpdate,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Update editable restaurant fields.
    delivery and active_days are controlled by the scraper, not here.
    """
    row = db.execute("""
        SELECT r.id FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ? AND r.slug = ?
    """, (city_slug, slug)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    updates, params = [], []
    if body.description     is not None: updates.append("description = ?");     params.append(body.description)
    if body.profile_picture is not None: updates.append("profile_picture = ?"); params.append(body.profile_picture)
    if body.verified        is not None: updates.append("verified = ?");        params.append(body.verified)
    if body.info            is not None: updates.append("info = ?");            params.append(body.info)
    if body.phone           is not None: updates.append("phone = ?");           params.append(body.phone)
    if body.address         is not None: updates.append("address = ?");         params.append(body.address)

    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    params.append(row["id"])
    db.execute(f"UPDATE restaurants SET {', '.join(updates)} WHERE id = ?", params)
    db.commit()

    return {"ok": True, "slug": slug, "city_slug": city_slug}


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("RESTAURANT_API_PORT", 6333))
    uvicorn.run("restaurant_api:app", host="0.0.0.0", port=port, reload=False)