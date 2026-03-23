# api.py
# FastAPI REST API for ToMenu — serves scraped lunch menu data.
# Admin dashboard is at / and /admin/*.
# Public API is at /api/*.
#
# Two databases:
#   main.db   — API keys, scrape audit log
#   namenu.db — cities, restaurants, menu items, scrape runs

import hashlib
import json
import os
import secrets
import subprocess
from datetime import date, timedelta
from typing import Optional

import sqlite3
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from scrapers.db import latest_run_id_for

# ── config ────────────────────────────────────────────────────────────────────

MAIN_DB   = os.environ.get("MAIN_DB",   "main.db")
NAMENU_DB = os.environ.get("NAMENU_DB", "namenu.db")

app = FastAPI(
    title="ToMenu API",
    description="Lunch menus from Slovak restaurants",
    version="0.2.0",
)

# ── db helpers ────────────────────────────────────────────────────────────────

def get_auth_db():
    conn = sqlite3.connect(MAIN_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_db():
    conn = sqlite3.connect(NAMENU_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


# ── auth ──────────────────────────────────────────────────────────────────────

def require_api_key(request: Request, auth_db: sqlite3.Connection = Depends(get_auth_db)):
    key = request.headers.get("Authorization", "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    row = auth_db.execute(
        "SELECT id FROM api_keys WHERE key_hash = ? AND active = 1", (key_hash,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Invalid or revoked API key")

    auth_db.execute(
        "UPDATE api_keys SET last_used = datetime('now') WHERE id = ?", (row["id"],)
    )
    auth_db.commit()
    return row["id"]


# ── serialization helpers ─────────────────────────────────────────────────────

def _parse_json(value, fallback=None):
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _item_to_dict(row) -> dict:
    d = dict(row) if not isinstance(row, dict) else row
    d["allergens"] = _parse_json(d.get("allergens"), [])
    d["tags"]      = _parse_json(d.get("tags"), [])
    # Drop the raw scrape text from public responses
    d.pop("raw", None)
    # Drop None macro fields to keep payload lean
    for macro in ("kcal", "protein_g", "fat_g", "carbs_g", "fiber_g"):
        if d.get(macro) is None:
            d.pop(macro, None)
    return d


def _restaurant_to_dict(row) -> dict:
    d = dict(row)
    d["active_days"] = _parse_json(d.get("active_days"), [])
    return d


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    for path in ("webUI/favicon.ico", "static/favicon.ico"):
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(status_code=404)

@app.get("/ml", include_in_schema=False)
def ml_dashboard():
    with open("webUI/ml.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)

@app.get("/", include_in_schema=False)
def dashboard():
    with open("webUI/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


# ── GET /api/cities ───────────────────────────────────────────────────────────

@app.get("/api/cities", summary="List all cities")
def list_cities(
    db:  sqlite3.Connection = Depends(get_db),
    _:   int                = Depends(require_api_key),
):
    """Returns all cities that have at least one scrape run."""
    rows = db.execute(
        "SELECT id, name, slug, url, lat, lon, restaurant_count FROM cities ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


# ── GET /api/cities/{city}/restaurants ───────────────────────────────────────

@app.get("/api/cities/{city}/restaurants", summary="List restaurants for a city")
def list_restaurants(
    city:     str,
    date:     Optional[str]  = Query(default=None, description="YYYY-MM-DD — defaults to today, falls back to most recent if no data"),
    delivery: Optional[bool] = Query(default=None),
    db:       sqlite3.Connection = Depends(get_db),
    _:        int                = Depends(require_api_key),
):
    target_date = date or _today()
    run_id, actual_date = latest_run_id_for(db, city, target_date)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"No menu data found for city '{city}'")

    q = """
        SELECT r.id, r.name, r.slug, r.address, r.phone,
               r.delivery, r.info, r.active_days, r.last_seen,
               r.description, r.profile_picture, r.verified,
               COUNT(m.id) AS item_count,
               MIN(m.menu_price) AS menu_price
        FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        LEFT JOIN menu_items m ON m.restaurant_id = r.id AND m.scrape_run_id = ?
        WHERE c.slug = ?
    """
    params = [run_id, city]

    if delivery is not None:
        q += " AND r.delivery = ?"
        params.append(1 if delivery else 0)

    q += " GROUP BY r.id ORDER BY r.name"
    rows = db.execute(q, params).fetchall()

    return {
        "city":        city,
        "date":        actual_date,
        "count":       len(rows),
        "restaurants": [_restaurant_to_dict(r) for r in rows],
    }


# ── GET /api/cities/{city}/restaurants/{slug} ─────────────────────────────────

@app.get("/api/cities/{city}/restaurants/{slug}", summary="Get one restaurant with its full menu")
def get_restaurant(
    city:  str,
    slug:  str,
    date:  Optional[str] = Query(default=None, description="YYYY-MM-DD — defaults to today, falls back to most recent"),
    db:    sqlite3.Connection = Depends(get_db),
    _:     int                = Depends(require_api_key),
):
    target_date = date or _today()
    run_id, actual_date = latest_run_id_for(db, city, target_date)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"No menu data found for city '{city}'")

    restaurant = db.execute("""
        SELECT r.* FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ? AND r.slug = ?
    """, (city, slug)).fetchone()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    items = db.execute("""
        SELECT type, name, description, weight, price_eur, menu_price,
               allergens, kcal, protein_g, fat_g, carbs_g, fiber_g, tags
        FROM menu_items
        WHERE restaurant_id = ? AND scrape_run_id = ?
        ORDER BY id
    """, (restaurant["id"], run_id)).fetchall()

    result = _restaurant_to_dict(restaurant)
    result["date"] = actual_date
    result["menu"] = [_item_to_dict(i) for i in items]
    return result


# ── GET /api/cities/{city}/menu ───────────────────────────────────────────────

@app.get("/api/cities/{city}/menu", summary="All dishes for a city, filterable")
def get_menu(
    city:              str,
    date:              Optional[str]   = Query(default=None, description="YYYY-MM-DD — defaults to today, falls back to most recent"),
    type:              Optional[str]   = Query(default=None, description="soup | main | dessert"),
    restaurant:        Optional[str]   = Query(default=None, description="Restaurant slug filter"),
    delivery:          Optional[bool]  = Query(default=None),
    exclude_allergens: Optional[str]   = Query(default=None, description="Comma-separated allergen numbers e.g. 1,7"),
    max_price:         Optional[float] = Query(default=None),
    tags:              Optional[str]   = Query(default=None, description="Comma-separated tags e.g. meat,fried"),
    limit:             int             = Query(default=50, le=200),
    offset:            int             = Query(default=0),
    db:                sqlite3.Connection = Depends(get_db),
    _:                 int                = Depends(require_api_key),
):
    target_date = date or _today()
    run_id, actual_date = latest_run_id_for(db, city, target_date)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"No menu data found for city '{city}'")

    q = """
        SELECT m.id, m.type, m.name, m.description, m.weight,
               m.price_eur, m.menu_price, m.allergens,
               m.kcal, m.protein_g, m.fat_g, m.carbs_g, m.fiber_g, m.tags,
               r.name AS restaurant_name, r.slug AS restaurant_slug,
               r.delivery, r.address
        FROM menu_items m
        JOIN restaurants r ON r.id = m.restaurant_id
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ? AND m.scrape_run_id = ?
    """
    params = [city, run_id]

    if type:
        q += " AND m.type = ?"
        params.append(type)
    if restaurant:
        q += " AND r.slug = ?"
        params.append(restaurant)
    if delivery is not None:
        q += " AND r.delivery = ?"
        params.append(1 if delivery else 0)
    if max_price is not None:
        q += " AND m.price_eur <= ?"
        params.append(max_price)
    if exclude_allergens:
        for a in [x.strip() for x in exclude_allergens.split(",") if x.strip().isdigit()]:
            q += " AND (m.allergens IS NULL OR m.allergens NOT LIKE ?)"
            params.append(f"%{a}%")
    if tags:
        for tag in [t.strip() for t in tags.split(",") if t.strip()]:
            q += " AND m.tags LIKE ?"
            params.append(f'%"{tag}"%')

    q += " ORDER BY r.name, m.id LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = db.execute(q, params).fetchall()
    return {
        "city":   city,
        "date":   actual_date,
        "count":  len(rows),
        "offset": offset,
        "items":  [_item_to_dict(r) for r in rows],
    }


# ── GET /api/cities/{city}/week ───────────────────────────────────────────────

@app.get("/api/cities/{city}/week", summary="Which weekdays have data this week")
def get_week(
    city:   str,
    source: Optional[str] = Query(default=None),
    db:     sqlite3.Connection = Depends(get_db),
    _:      int                = Depends(require_api_key),
):
    today  = _today_date()
    monday = today - timedelta(days=today.weekday())
    week_dates = [(monday + timedelta(days=i)).isoformat() for i in range(5)]

    result = []
    for d in week_dates:
        run_id, _ = latest_run_id_for(db, city, d, source)
        count = 0
        if run_id:
            count = db.execute(
                "SELECT COUNT(*) FROM menu_items WHERE scrape_run_id=?", (run_id,)
            ).fetchone()[0]
        result.append({"date": d, "has_data": run_id is not None, "item_count": count})
    return result


# ── GET /api/feed ─────────────────────────────────────────────────────────────

@app.get("/api/feed", summary="Personalised ranked dish feed for a user")
def get_feed(
    city:    str,
    user_id: int,
    date:    Optional[str] = Query(default=None),
    limit:   int           = Query(default=20, le=50),
    weights: Optional[str] = Query(default=None, description="tag:score,tag:score — e.g. meat:80,vegetarian:-60"),
    db:      sqlite3.Connection = Depends(get_db),
    _:       int                = Depends(require_api_key),
):
    """
    Returns a ranked list of dishes for the FYP card stack.

    When weights are provided (from the feed preview or a taste profile),
    items are scored by summing the weight for each matching tag, then
    ranked descending. Items with no weight signal are ranked last but
    still included (score = 0).
    """
    target_date = date or _today()
    run_id, actual_date = latest_run_id_for(db, city, target_date)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"No menu data found for city '{city}'")

    rows = db.execute("""
        SELECT m.id, m.type, m.name, m.description, m.weight,
               m.price_eur, m.menu_price, m.allergens,
               m.kcal, m.protein_g, m.fat_g, m.carbs_g, m.fiber_g, m.tags,
               r.name AS restaurant_name, r.slug AS restaurant_slug,
               r.delivery, r.address
        FROM menu_items m
        JOIN restaurants r ON r.id = m.restaurant_id
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ? AND m.scrape_run_id = ? AND m.type = 'main'
    """, (city, run_id)).fetchall()

    items = [_item_to_dict(r) for r in rows]

    # Parse weight map from query param: "meat:80,vegetarian:-60"
    weight_map: dict[str, float] = {}
    if weights:
        for part in weights.split(","):
            part = part.strip()
            if ":" in part:
                tag, val = part.rsplit(":", 1)
                try:
                    weight_map[tag.strip()] = float(val.strip())
                except ValueError:
                    pass

    if weight_map:
        def score(item: dict) -> float:
            item_tags = item.get("tags") or []
            return sum(weight_map.get(t, 0) for t in item_tags)

        items.sort(key=score, reverse=True)
    else:
        # No weights — shuffle so it's not always the same order
        import random
        random.shuffle(items)

    return {
        "city":    city,
        "date":    actual_date,
        "user_id": user_id,
        "count":   len(items[:limit]),
        "items":   items[:limit],
    }


# ── admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin/stats")
def admin_stats(
    city:     Optional[str] = Query(default=None),
    db:       sqlite3.Connection = Depends(get_db),
    auth_db:  sqlite3.Connection = Depends(get_auth_db),
    _:        int                = Depends(require_api_key),
):
    today       = _today()
    city_clause = "AND c.slug = ?" if city else ""
    city_params = [city] if city else []

    restaurants = db.execute(
        f"SELECT COUNT(*) FROM restaurants r JOIN cities c ON c.id = r.city_id WHERE 1=1 {city_clause}",
        city_params
    ).fetchone()[0]

    delivery = db.execute(
        f"SELECT COUNT(*) FROM restaurants r JOIN cities c ON c.id = r.city_id WHERE r.delivery=1 {city_clause}",
        city_params
    ).fetchone()[0]

    scrape_runs = db.execute(
        f"SELECT COUNT(*) FROM scrape_runs sr JOIN cities c ON c.id = sr.city_id WHERE 1=1 {city_clause}",
        city_params
    ).fetchone()[0]

    # FIX: items_today counts items from the MOST RECENT scrape run per city,
    # not just runs where date == today. This means data scraped on Monday
    # still shows correctly on Tuesday, Wednesday, etc.
    if city:
        run_id, _ = latest_run_id_for(db, city, today)
        items_today = db.execute(
            "SELECT COUNT(*) FROM menu_items WHERE scrape_run_id = ?", (run_id,)
        ).fetchone()[0] if run_id else 0
    else:
        # Sum across all cities using their latest run
        all_cities = db.execute("SELECT slug FROM cities").fetchall()
        items_today = 0
        for c_row in all_cities:
            run_id, _ = latest_run_id_for(db, c_row[0], today)
            if run_id:
                cnt = db.execute(
                    "SELECT COUNT(*) FROM menu_items WHERE scrape_run_id = ?", (run_id,)
                ).fetchone()[0]
                items_today += cnt

    recent_runs = db.execute(f"""
        SELECT c.name AS city_name, c.slug AS city_slug,
               sr.date, sr.scraped_at, COUNT(m.id) AS item_count
        FROM scrape_runs sr
        JOIN cities c ON c.id = sr.city_id
        LEFT JOIN menu_items m ON m.scrape_run_id = sr.id
        WHERE 1=1 {city_clause}
        GROUP BY sr.id ORDER BY sr.id DESC LIMIT 15
    """, city_params).fetchall()

    # FIX: city_breakdown also uses latest_run_id_for instead of date == today
    all_cities = db.execute("SELECT name, slug, restaurant_count FROM cities ORDER BY name").fetchall()
    city_breakdown = []
    for c_row in all_cities:
        run_id, _ = latest_run_id_for(db, c_row["slug"], today)
        items_cnt = 0
        if run_id:
            items_cnt = db.execute(
                "SELECT COUNT(*) FROM menu_items WHERE scrape_run_id = ?", (run_id,)
            ).fetchone()[0]
        city_breakdown.append({
            "name":             c_row["name"],
            "slug":             c_row["slug"],
            "restaurant_count": c_row["restaurant_count"],
            "items_today":      items_cnt,
        })

    scrape_log = auth_db.execute(
        "SELECT * FROM scrape_log ORDER BY id DESC LIMIT 10"
    ).fetchall()

    return {
        "restaurants":    restaurants,
        "delivery":       delivery,
        "items_today":    items_today,
        "scrape_runs":    scrape_runs,
        "recent_runs":    [dict(r) for r in recent_runs],
        "city_breakdown": city_breakdown,
        "scrape_log":     [dict(r) for r in scrape_log],
    }


@app.get("/admin/runs")
def admin_runs(
    city: Optional[str] = Query(default=None),
    db:   sqlite3.Connection = Depends(get_db),
    _:    int                = Depends(require_api_key),
):
    city_clause = "AND c.slug = ?" if city else ""
    city_params = [city] if city else []
    rows = db.execute(f"""
        SELECT c.name AS city_name, c.slug AS city_slug,
               sr.date, sr.scraped_at, COUNT(m.id) AS item_count
        FROM scrape_runs sr
        JOIN cities c ON c.id = sr.city_id
        LEFT JOIN menu_items m ON m.scrape_run_id = sr.id
        WHERE 1=1 {city_clause}
        GROUP BY sr.id ORDER BY sr.id DESC LIMIT 50
    """, city_params).fetchall()
    return [dict(r) for r in rows]


@app.get("/admin/restaurants")
def admin_restaurants(
    city: Optional[str] = Query(default=None),
    db:   sqlite3.Connection = Depends(get_db),
    _:    int                = Depends(require_api_key),
):
    city_clause = "AND c.slug = ?" if city else ""
    city_params = [city] if city else []
    rows = db.execute(f"""
        SELECT r.*, c.name AS city_name, c.slug AS city_slug
        FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        WHERE 1=1 {city_clause}
        ORDER BY c.name, r.name
    """, city_params).fetchall()
    return [_restaurant_to_dict(r) for r in rows]


@app.get("/admin/menus")
def admin_menus(
    city:    Optional[str] = Query(default=None),
    on_date: Optional[str] = Query(default=None, alias="date"),
    db:      sqlite3.Connection = Depends(get_db),
    _:       int                = Depends(require_api_key),
):
    if not city:
        raise HTTPException(status_code=400, detail="city parameter is required")

    target_date = on_date or _today()
    run_id, actual_date = latest_run_id_for(db, city, target_date)
    if not run_id:
        return {"restaurants": [], "city": city, "date": target_date}

    restaurants = db.execute("""
        SELECT r.* FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ? ORDER BY r.name
    """, (city,)).fetchall()

    result = []
    for r in restaurants:
        items = db.execute("""
            SELECT type, name, description, weight, price_eur, menu_price,
                   allergens, kcal, protein_g, fat_g, carbs_g, fiber_g, tags
            FROM menu_items WHERE restaurant_id=? AND scrape_run_id=? ORDER BY id
        """, (r["id"], run_id)).fetchall()
        if not items: continue
        d = _restaurant_to_dict(r)
        d["menu_price"] = items[0]["menu_price"]
        d["menu"] = [_item_to_dict(i) for i in items]
        result.append(d)

    return {"restaurants": result, "city": city, "date": actual_date}


@app.post("/admin/scrape")
def admin_scrape(
    db:      sqlite3.Connection = Depends(get_db),
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    cur    = auth_db.execute(
        "INSERT INTO scrape_log (source, started_at, status) VALUES ('namenu', datetime('now'), 'running')"
    )
    log_id = cur.lastrowid
    auth_db.commit()

    try:
        result = subprocess.run(
            ["python", "-X", "utf8", "scrapers/namenu.scrape.py", "--today"],
            capture_output=True, text=True, timeout=300, encoding="utf-8",
        )
        if result.returncode != 0:
            auth_db.execute(
                "UPDATE scrape_log SET finished_at=datetime('now'), status='error', error=? WHERE id=?",
                (result.stderr or "scrape failed", log_id),
            )
            auth_db.commit()
            _ntfy_notify(auth_db, f"Scrape failed: {result.stderr or 'unknown error'}", title="ToMenu Scrape Error")
            raise HTTPException(status_code=500, detail=result.stderr or "scrape failed")

        run    = db.execute("SELECT id FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
        run_id = run[0] if run else None
        items_scraped = db.execute(
            "SELECT COUNT(*) FROM menu_items WHERE scrape_run_id >= "
            "(SELECT MIN(id) FROM scrape_runs WHERE date = ?)",
            (_today(),),
        ).fetchone()[0] if run_id else 0

        auth_db.execute(
            "UPDATE scrape_log SET finished_at=datetime('now'), status='ok', items=? WHERE id=?",
            (items_scraped, log_id),
        )
        auth_db.commit()
        if items_scraped == 0:
            _ntfy_notify(auth_db, "Scrape completed but 0 items were saved — check the source site", title="ToMenu Warning")
        return {"ok": True, "run_id": run_id, "items_scraped": items_scraped, "output": result.stdout}

    except subprocess.TimeoutExpired:
        auth_db.execute(
            "UPDATE scrape_log SET finished_at=datetime('now'), status='error', error='timeout' WHERE id=?",
            (log_id,),
        )
        auth_db.commit()
        _ntfy_notify(auth_db, "Scrape timed out after 5 minutes", title="ToMenu Scrape Error")
        raise HTTPException(status_code=504, detail="scrape timed out")


class NewKeyRequest(BaseModel):
    label: str


@app.post("/admin/keys")
def admin_create_key(
    body:    NewKeyRequest,
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    key      = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    auth_db.execute(
        "INSERT INTO api_keys (key_hash, label, created_at) VALUES (?, ?, datetime('now'))",
        (key_hash, body.label),
    )
    auth_db.commit()
    return {"key": key, "label": body.label}


@app.get("/admin/keys")
def admin_list_keys(
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    rows = auth_db.execute(
        "SELECT id, label, created_at, last_used, active FROM api_keys ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/admin/keys/{key_id}")
def admin_revoke_key(
    key_id:  int,
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    auth_db.execute("UPDATE api_keys SET active=0 WHERE id=?", (key_id,))
    auth_db.commit()
    return {"ok": True}

@app.get("/admin/tags")
def admin_tags(
    city: Optional[str] = Query(default=None),
    db:   sqlite3.Connection = Depends(get_db),
    _:    int                = Depends(require_api_key),
):
    """
    Returns tag distribution across all menu items.
    Each entry: { tag, count, pct }
    """
    city_clause = "AND c.slug = ?" if city else ""
    city_params = [city] if city else []

    rows = db.execute(f"""
        SELECT m.tags FROM menu_items m
        JOIN restaurants r ON r.id = m.restaurant_id
        JOIN cities c ON c.id = r.city_id
        WHERE m.tags IS NOT NULL AND m.tags != '[]'
        {city_clause}
    """, city_params).fetchall()

    total = db.execute(f"""
        SELECT COUNT(*) FROM menu_items m
        JOIN restaurants r ON r.id = m.restaurant_id
        JOIN cities c ON c.id = r.city_id
        WHERE 1=1 {city_clause}
    """, city_params).fetchone()[0]

    counts: dict[str, int] = {}
    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except Exception:
            continue
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + 1

    result = sorted(
        [{"tag": t, "count": c, "pct": round(c / total * 100, 1) if total else 0}
         for t, c in counts.items()],
        key=lambda x: -x["count"],
    )
    return {"total_items": total, "tags": result}

@app.get("/admin/tags/{tag}")
def admin_tag_detail(
    tag:  str,
    city: Optional[str] = Query(default=None),
    limit: int          = Query(default=50, le=200),
    db:   sqlite3.Connection = Depends(get_db),
    _:    int                = Depends(require_api_key),
):
    """
    Returns up to `limit` menu items that carry the given tag.
    Useful for auditing whether the tagger is working correctly.
    """
    city_clause = "AND c.slug = ?" if city else ""
    city_params = [city] if city else []

    rows = db.execute(f"""
        SELECT m.id, m.type, m.name, m.description, m.tags,
               m.allergens,
               r.name AS restaurant_name, r.slug AS restaurant_slug,
               c.slug AS city_slug
        FROM menu_items m
        JOIN restaurants r ON r.id = m.restaurant_id
        JOIN cities c ON c.id = r.city_id
        WHERE m.tags LIKE ? {city_clause}
        ORDER BY m.id DESC
        LIMIT ?
    """, [f'%"{tag}"%'] + city_params + [limit]).fetchall()

    if not rows and not city:
        raise HTTPException(status_code=404, detail=f"No items found with tag '{tag}'")

    return {
        "tag":   tag,
        "count": len(rows),
        "items": [_item_to_dict(dict(r)) for r in rows],
    }

class PatchRestaurantRequest(BaseModel):
    description:     Optional[str] = None
    profile_picture: Optional[str] = None
    verified:        Optional[bool] = None
    info:            Optional[str] = None
    phone:           Optional[str] = None
    address:         Optional[str] = None

@app.patch("/admin/restaurants/{city}/{slug}")
def admin_patch_restaurant(
    city:    str,
    slug:    str,
    body:    PatchRestaurantRequest,
    db:      sqlite3.Connection = Depends(get_db),
    _:       int                = Depends(require_api_key),
):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    restaurant = db.execute("""
        SELECT r.id FROM restaurants r
        JOIN cities c ON c.id = r.city_id
        WHERE c.slug = ? AND r.slug = ?
    """, (city, slug)).fetchone()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE restaurants SET {set_clause} WHERE id = ?",
        list(updates.values()) + [restaurant["id"]],
    )
    db.commit()
    return {"ok": True, "updated": list(updates.keys())}


@app.get("/admin/system")
def admin_system(
    db:      sqlite3.Connection = Depends(get_db),
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    """System-level stats: DB sizes, row counts, cron schedule."""
    cities       = db.execute("SELECT COUNT(*) FROM cities").fetchone()[0]
    restaurants  = db.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    scrape_runs  = db.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]
    menu_items   = db.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0]
    api_keys     = auth_db.execute("SELECT COUNT(*) FROM api_keys WHERE active=1").fetchone()[0]

    namenu_size = os.path.getsize(NAMENU_DB) if os.path.exists(NAMENU_DB) else 0
    main_size   = os.path.getsize(MAIN_DB)   if os.path.exists(MAIN_DB)   else 0

    cron_row = auth_db.execute(
        "SELECT schedule FROM cron_schedule WHERE id=1"
    ).fetchone()
    cron_schedule = cron_row["schedule"] if cron_row else "0 6,12,18,0 * * 1-5"

    return {
        "cities":        cities,
        "restaurants":   restaurants,
        "scrape_runs":   scrape_runs,
        "menu_items":    menu_items,
        "api_keys":      api_keys,
        "namenu_db_bytes": namenu_size,
        "main_db_bytes":   main_size,
        "cron_schedule": cron_schedule,
    }

class CronRequest(BaseModel):
    schedule: str

@app.patch("/admin/cron")
def admin_set_cron(
    body:    CronRequest,
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    schedule = body.schedule.strip()
    if len(schedule.split()) != 5:
        raise HTTPException(status_code=422, detail="schedule must be 5 cron fields")
    auth_db.execute("""
        INSERT INTO cron_schedule (id, schedule, updated_at)
        VALUES (1, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET schedule=excluded.schedule, updated_at=excluded.updated_at
    """, (schedule,))
    auth_db.commit()
    return {"ok": True, "schedule": schedule}

# ── bug reporting ─────────────────────────────────────────────────────────────
# Public endpoint — called by app clients, no API key required.
# Types: wrong_tag, wrong_price, menu_error, profile_error

class BugReportRequest(BaseModel):
    city_slug:       str
    restaurant_slug: str
    item_id:         Optional[int] = None
    type:            str
    description:     Optional[str] = None


@app.post("/bugs", summary="Submit a bug report from the app")
def submit_bug(body: BugReportRequest, auth_db: sqlite3.Connection = Depends(get_auth_db)):
    valid_types = {"wrong_tag", "wrong_price", "menu_error", "profile_error"}
    if body.type not in valid_types:
        raise HTTPException(status_code=422, detail=f"type must be one of {valid_types}")

    auth_db.execute("""
        INSERT INTO bugs (city_slug, restaurant_slug, item_id, type, description, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', datetime('now'))
    """, (body.city_slug, body.restaurant_slug, body.item_id, body.type, body.description))
    auth_db.commit()

    _ntfy_notify(auth_db, f"New bug reported: {body.type} at {body.restaurant_slug} ({body.city_slug})")
    return {"ok": True}


@app.get("/admin/bugs", summary="List bug reports")
def admin_list_bugs(
    status:  Optional[str] = Query(default=None, description="open | urgent | resolved"),
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    clause = "WHERE status = ?" if status else ""
    params = [status] if status else []
    rows = auth_db.execute(
        f"SELECT * FROM bugs {clause} ORDER BY id DESC LIMIT 100", params
    ).fetchall()
    return [dict(r) for r in rows]


class BugPatchRequest(BaseModel):
    status:  Optional[str] = None
    notes:   Optional[str] = None


@app.patch("/admin/bugs/{bug_id}", summary="Update bug status")
def admin_patch_bug(
    bug_id:  int,
    body:    BugPatchRequest,
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    valid_statuses = {"open", "urgent", "resolved"}
    if body.status and body.status not in valid_statuses:
        raise HTTPException(status_code=422, detail=f"status must be one of {valid_statuses}")

    auth_db.execute(f"""
        UPDATE bugs SET
            status = COALESCE(?, status),
            resolved_at = CASE WHEN ? = 'resolved' THEN datetime('now') ELSE resolved_at END
        WHERE id = ?
    """, (body.status, body.status, bug_id))
    auth_db.commit()
    return {"ok": True}

# ── health check (runs tester.py --json) ──────────────────────────────────────

@app.get("/admin/health-check", summary="Run full API test suite and return results")
def admin_health_check(
    _: int = Depends(require_api_key),
):
    """
    Shells out to tester.py --json and returns structured test results.
    Takes ~3-5 seconds. Results are not cached — each call runs fresh.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["python", "-X", "utf8", "tester.py", "--json"],
            capture_output=True, text=True, timeout=60, encoding="utf-8",
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=500,
                detail=f"tester.py returned non-JSON output: {result.stdout[:500]}"
            )
        return data
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="tester.py timed out after 60s")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="tester.py not found — run from backend root")


# ── menu item tag patch ────────────────────────────────────────────────────────

class TagPatchRequest(BaseModel):
    tags:          list[str]
    training_example: bool = False   # if True, record as confirmed training data


@app.patch("/admin/menu-items/{item_id}/tags", summary="Update tags on a menu item")
def admin_patch_item_tags(
    item_id: int,
    body:    TagPatchRequest,
    db:      sqlite3.Connection = Depends(get_db),
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    """
    Overwrites the tags JSON array on a menu item.
    If training_example=True, also records this in ml_training_examples
    so the ML system knows these tags were human-verified.
    """
    item = db.execute("SELECT id, name FROM menu_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail=f"Menu item {item_id} not found")

    tags_json = json.dumps(sorted(set(body.tags)), ensure_ascii=False)
    db.execute("UPDATE menu_items SET tags = ? WHERE id = ?", (tags_json, item_id))
    db.commit()

    if body.training_example:
        # Ensure table exists with the correct schema
        db.execute("""
            CREATE TABLE IF NOT EXISTS ml_training_examples (
                id          INTEGER PRIMARY KEY,
                item_id     INTEGER NOT NULL,
                item_name   TEXT,
                tags        TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Add unique index if it doesn't exist yet — safe on existing DBs
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_examples_item_id
            ON ml_training_examples(item_id)
        """)
        db.commit()
        # DELETE + INSERT is the most compatible upsert pattern for SQLite
        db.execute(
            "DELETE FROM ml_training_examples WHERE item_id = ?", (item_id,)
        )
        db.execute("""
            INSERT INTO ml_training_examples (item_id, item_name, tags, created_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (item_id, item["name"], tags_json))
        db.commit()

    return {"ok": True, "item_id": item_id, "tags": json.loads(tags_json)}


@app.get("/admin/menu-items/search", summary="Search menu items by dish name for tag editing")
def admin_search_items(
    q:      str = Query(default="%", min_length=1, description="Search string — use % or omit for all recent items"),
    city:   Optional[str] = Query(default=None),
    limit:  int = Query(default=30, le=100),
    db:     sqlite3.Connection = Depends(get_db),
    _:      int = Depends(require_api_key),
):
    """
    Returns menu items matching the name query.
    Pass q=% (or leave empty) to get the most recent items with no filter.
    Used by the tag editor in the ML/Tags page.
    """
    city_clause = "AND c.slug = ?" if city else ""
    city_params = [city] if city else []
    # Bare % means match all — used by the tag editor auto-load on tab open
    search_pattern = f"%{q}%" if q != "%" else "%"

    rows = db.execute(f"""
        SELECT m.id, m.type, m.name, m.description, m.tags, m.allergens,
               r.name AS restaurant_name, r.slug AS restaurant_slug,
               c.slug AS city_slug
        FROM menu_items m
        JOIN restaurants r ON r.id = m.restaurant_id
        JOIN cities c ON c.id = r.city_id
        WHERE m.name LIKE ? {city_clause}
        ORDER BY m.id DESC
        LIMIT ?
    """, [search_pattern] + city_params + [limit]).fetchall()

    return {
        "count": len(rows),
        "items": [_item_to_dict(dict(r)) for r in rows],
    }


# ── admin scrape with day selection ───────────────────────────────────────────

class ScrapeRequest(BaseModel):
    days: Optional[list[str]] = None  # e.g. ["pondelok","streda"] — None = today


@app.post("/admin/scrape-days", summary="Scrape specific weekdays")
def admin_scrape_days(
    body:    ScrapeRequest,
    db:      sqlite3.Connection = Depends(get_db),
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    """
    Scrapes specific weekdays. body.days = list of Slovak day slugs.
    If days is None or empty, falls back to today.
    """
    import subprocess

    valid = {"pondelok", "utorok", "streda", "stvrtok", "piatok"}
    days  = [d for d in (body.days or []) if d in valid]

    if not days:
        cmd = ["python", "-X", "utf8", "scrapers/namenu.scrape.py", "--today"]
        label = "today"
    elif len(days) == 1:
        cmd   = ["python", "-X", "utf8", "scrapers/namenu.scrape.py", "--day", days[0]]
        label = days[0]
    else:
        cmd   = ["python", "-X", "utf8", "scrapers/namenu.scrape.py"] + [f"--day {d}" for d in days]
        label = "+".join(days)

    cur    = auth_db.execute(
        "INSERT INTO scrape_log (source, started_at, status) VALUES (?, datetime('now'), 'running')",
        (f"namenu/{label}",)
    )
    log_id = cur.lastrowid
    auth_db.commit()

    combined_output = ""
    error_msg       = None

    try:
        if len(days) > 1:
            for d in days:
                r = subprocess.run(
                    ["python", "-X", "utf8", "scrapers/namenu.scrape.py", "--day", d],
                    capture_output=True, text=True, timeout=300, encoding="utf-8",
                )
                combined_output += f"\n--- {d} ---\n" + (r.stdout or "")
                if r.returncode != 0:
                    error_msg = r.stderr or f"scrape failed for {d}"
                    break
        else:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, encoding="utf-8",
            )
            combined_output = r.stdout or ""
            if r.returncode != 0:
                error_msg = r.stderr or "scrape failed"

        if error_msg:
            auth_db.execute(
                "UPDATE scrape_log SET finished_at=datetime('now'), status='error', error=? WHERE id=?",
                (error_msg, log_id),
            )
            auth_db.commit()
            raise HTTPException(status_code=500, detail=error_msg)

        run = db.execute("SELECT id FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
        run_id = run[0] if run else None

        auth_db.execute(
            "UPDATE scrape_log SET finished_at=datetime('now'), status='ok', items=0 WHERE id=?",
            (log_id,)
        )
        auth_db.commit()
        return {"ok": True, "run_id": run_id, "days": days or ["today"], "output": combined_output}

    except subprocess.TimeoutExpired:
        auth_db.execute(
            "UPDATE scrape_log SET finished_at=datetime('now'), status='error', error='timeout' WHERE id=?",
            (log_id,)
        )
        auth_db.commit()
        raise HTTPException(status_code=504, detail="scrape timed out")

# ── ntfy config ───────────────────────────────────────────────────────────────

@app.get("/admin/ntfy", summary="Get ntfy config")
def admin_get_ntfy(
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    row = auth_db.execute("SELECT * FROM ntfy_config WHERE id = 1").fetchone()
    if not row:
        return {
            "server_url": "https://ntfy.sh",
            "topic":      "tomenu-admin",
            "private":    False,
        }
    return {
        "server_url": row["server_url"],
        "topic":      row["topic"],
        "private":    bool(row["private"]),
        "updated_at": row["updated_at"],
    }

class NtfyConfigRequest(BaseModel):
    server_url: str
    topic:      str
    private:    bool = False
    auth_token: Optional[str] = None


@app.patch("/admin/ntfy", summary="Save ntfy config")
def admin_set_ntfy(
    body:    NtfyConfigRequest,
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    auth_db.execute("""
        INSERT INTO ntfy_config (id, server_url, topic, private, auth_token, updated_at)
        VALUES (1, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            server_url = excluded.server_url,
            topic      = excluded.topic,
            private    = excluded.private,
            auth_token = COALESCE(excluded.auth_token, auth_token),
            updated_at = excluded.updated_at
    """, (body.server_url, body.topic, 1 if body.private else 0, body.auth_token))
    auth_db.commit()
    return {"ok": True}


# ── ntfy helper ───────────────────────────────────────────────────────────────

def _ntfy_notify(auth_db: sqlite3.Connection, message: str, title: str = "ToMenu Admin") -> tuple[bool, str]:
    """
    Send an ntfy notification. Returns (success, error_message).
    Uses httpx (already in requirements) — never imports requests.
    """
    try:
        import httpx as _hx
        row = auth_db.execute("SELECT * FROM ntfy_config WHERE id = 1").fetchone()
        if not row:
            return False, "ntfy not configured"
        url     = f"{row['server_url'].rstrip('/')}/{row['topic']}"
        headers = {"Title": title, "Content-Type": "text/plain"}
        if row["private"] and row["auth_token"]:
            headers["Authorization"] = f"Bearer {row['auth_token']}"
        r = _hx.post(url, content=message.encode("utf-8"), headers=headers, timeout=5)
        if r.is_success:
            return True, ""
        return False, f"ntfy returned {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


@app.post("/admin/ntfy/test", summary="Send a test ntfy notification")
def admin_test_ntfy(
    auth_db: sqlite3.Connection = Depends(get_auth_db),
    _:       int                = Depends(require_api_key),
):
    """Sends a real test notification and returns success/failure detail."""
    ok, err = _ntfy_notify(auth_db, "ToMenu test notification — if you see this, ntfy is working ✓", title="ToMenu Test")
    if not ok:
        raise HTTPException(status_code=500, detail=err or "ntfy send failed")
    return {"ok": True, "message": "test notification sent"}


# ── ML endpoints ─────────────────────────────────────────────────────────────

@app.post("/admin/ml/train", summary="Train the ML tag model on all tagged menu items")
def admin_ml_train(
    db:      sqlite3.Connection = Depends(get_db),
    _:       int                = Depends(require_api_key),
):
    """
    Trains (or retrains) the ML tagger on:
      • All menu_items that have at least one tag (weak supervision)
      • ml_training_examples from the Bulk Editor (human-verified, weighted 5×)

    Returns training stats. Safe to call multiple times — each call overwrites
    the previous model.
    """
    try:
        from ml.tagger import train as ml_train
    except ImportError:
        raise HTTPException(status_code=500, detail="ml/tagger.py not found — check your installation")

    result = ml_train(NAMENU_DB)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "training failed"))
    return result


@app.get("/admin/ml/status", summary="Get ML model metadata and training stats")
def admin_ml_status(
    _: int = Depends(require_api_key),
):
    """Returns metadata about the current trained model, or 'not trained' status."""
    try:
        from ml.tagger import model_status
    except ImportError:
        return {"trained": False, "message": "ml/tagger.py not found"}
    return model_status()


@app.get("/admin/ml/predict", summary="Run ML tag prediction on a single dish")
def admin_ml_predict(
    name:        str = Query(..., description="Dish name in Slovak"),
    dish_type:   str = Query(default="main", description="main | soup | dessert"),
    description: str = Query(default="", description="Optional description"),
    _:           int = Depends(require_api_key),
):
    """
    Predict tags for a single dish using the current model.
    Returns tags, per-tag confidence scores, and which source was used (ml+rules / rules).
    """
    try:
        from ml.tagger import predict as ml_predict
    except ImportError:
        raise HTTPException(status_code=500, detail="ml/tagger.py not found")
    return ml_predict(name, dish_type, description, return_scores=True)


@app.get("/admin/ml/examples", summary="List training examples saved via the bulk editor")
def admin_ml_examples(
    limit:  int = Query(default=50, le=200),
    db:     sqlite3.Connection = Depends(get_db),
    _:      int = Depends(require_api_key),
):
    """Returns the most recent human-verified training examples."""
    try:
        rows = db.execute("""
            SELECT id, item_id, item_name, tags, created_at
            FROM ml_training_examples
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """, (limit,)).fetchall()
    except Exception:
        return {"count": 0, "examples": [], "note": "ml_training_examples table does not exist yet"}

    examples = []
    for r in rows:
        d = dict(r)
        try:
            d["tags"] = json.loads(d["tags"])
        except Exception:
            d["tags"] = []
        examples.append(d)

    return {"count": len(examples), "examples": examples}


# ── small utilities ───────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()

def _today_date() -> date:
    return date.today()