# scrapers/db.py
# Shared database helpers for all ToMenu scrapers.
# This file only touches namenu.db (scraped menu data).
# API keys and scrape_log live in main.db — see main.py.

import sqlite3
import json
import os

NAMENU_DB = os.environ.get("NAMENU_DB", "namenu.db")


def connect():
    conn = sqlite3.connect(NAMENU_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cities (
            id               INTEGER PRIMARY KEY,
            name             TEXT NOT NULL,
            slug             TEXT NOT NULL UNIQUE,
            url              TEXT NOT NULL,
            lat              REAL,
            lon              REAL,
            restaurant_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS restaurants (
            id              INTEGER PRIMARY KEY,
            city_id         INTEGER NOT NULL REFERENCES cities(id),
            name            TEXT NOT NULL,
            slug            TEXT NOT NULL,
            url             TEXT,
            address         TEXT,
            phone           TEXT,
            delivery        INTEGER NOT NULL DEFAULT 0,
            info            TEXT,
            description     TEXT,
            profile_picture TEXT,
            verified        INTEGER NOT NULL DEFAULT 0,
            active_days     TEXT NOT NULL DEFAULT '[]',
            last_seen       TEXT,
            UNIQUE(city_id, slug)
        );

        CREATE TABLE IF NOT EXISTS scrape_runs (
            id         INTEGER PRIMARY KEY,
            city_id    INTEGER NOT NULL REFERENCES cities(id),
            source     TEXT    NOT NULL DEFAULT 'namenu',
            scraped_at TEXT    NOT NULL,
            date       TEXT    NOT NULL,
            UNIQUE(city_id, source, date)
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id            INTEGER PRIMARY KEY,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            scrape_run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
            type          TEXT NOT NULL CHECK(type IN ('soup','main','dessert')),
            name          TEXT,
            description   TEXT,
            weight        TEXT,
            price_eur     REAL,
            menu_price    REAL,
            allergens     TEXT,
            kcal          REAL,
            protein_g     REAL,
            fat_g         REAL,
            carbs_g       REAL,
            fiber_g       REAL,
            tags          TEXT NOT NULL DEFAULT '[]',
            raw           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_menu_items_run   ON menu_items(scrape_run_id);
        CREATE INDEX IF NOT EXISTS idx_menu_items_rest  ON menu_items(restaurant_id);
        CREATE INDEX IF NOT EXISTS idx_menu_items_type  ON menu_items(type);
        CREATE INDEX IF NOT EXISTS idx_menu_items_tags  ON menu_items(tags);
        CREATE INDEX IF NOT EXISTS idx_scrape_runs_date ON scrape_runs(date);
        CREATE INDEX IF NOT EXISTS idx_scrape_runs_src  ON scrape_runs(source);
        CREATE INDEX IF NOT EXISTS idx_restaurants_city ON restaurants(city_id);
        CREATE INDEX IF NOT EXISTS idx_restaurants_slug ON restaurants(city_id, slug);
    """)

    _migrate(conn)
    conn.commit()


def _migrate(conn):
    """Non-destructive migrations for existing databases."""

    # ── cities ────────────────────────────────────────────────────────────────
    city_cols = {r[1] for r in conn.execute("PRAGMA table_info(cities)").fetchall()}
    if "lat" not in city_cols:
        conn.execute("ALTER TABLE cities ADD COLUMN lat REAL")
        print("  [db] migrated: added lat to cities")
    if "lon" not in city_cols:
        conn.execute("ALTER TABLE cities ADD COLUMN lon REAL")
        print("  [db] migrated: added lon to cities")
    if "restaurant_count" not in city_cols:
        conn.execute("ALTER TABLE cities ADD COLUMN restaurant_count INTEGER NOT NULL DEFAULT 0")
        print("  [db] migrated: added restaurant_count to cities")

    # ── restaurants ───────────────────────────────────────────────────────────
    rest_cols = {r[1] for r in conn.execute("PRAGMA table_info(restaurants)").fetchall()}
    for col, defn in [
        ("description",     "TEXT"),
        ("profile_picture", "TEXT"),
        ("verified",        "INTEGER NOT NULL DEFAULT 0"),
        ("active_days",     "TEXT NOT NULL DEFAULT '[]'"),
        ("last_seen",       "TEXT"),
    ]:
        if col not in rest_cols:
            conn.execute(f"ALTER TABLE restaurants ADD COLUMN {col} {defn}")
            print(f"  [db] migrated: added {col} to restaurants")

    # ── scrape_runs: drop legacy 'day' text column is not possible in SQLite,
    #    but we stop writing it. Just ensure source column exists.
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(scrape_runs)").fetchall()}
    if "source" not in run_cols:
        conn.execute("ALTER TABLE scrape_runs ADD COLUMN source TEXT NOT NULL DEFAULT 'namenu'")
        print("  [db] migrated: added source to scrape_runs")

    # ── menu_items: flat macros + tags ────────────────────────────────────────
    item_cols = {r[1] for r in conn.execute("PRAGMA table_info(menu_items)").fetchall()}
    for col, defn in [
        ("kcal",      "REAL"),
        ("protein_g", "REAL"),
        ("fat_g",     "REAL"),
        ("carbs_g",   "REAL"),
        ("fiber_g",   "REAL"),
        ("tags",      "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        if col not in item_cols:
            conn.execute(f"ALTER TABLE menu_items ADD COLUMN {col} {defn}")
            print(f"  [db] migrated: added {col} to menu_items")

    # Back-fill tags from existing nutrition JSON blob if tags column was just added
    if "tags" not in item_cols:
        print("  [db] tags column is new — all existing rows start with empty tags []")

    conn.commit()


# ── city helpers ──────────────────────────────────────────────────────────────

def get_or_create_city(conn, name, slug, url, lat=None, lon=None):
    row = conn.execute("SELECT id FROM cities WHERE slug = ?", (slug,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO cities (name, slug, url, lat, lon) VALUES (?, ?, ?, ?, ?)",
        (name, slug, url, lat, lon)
    )
    conn.commit()
    return cur.lastrowid


def update_city_restaurant_count(conn, city_id):
    count = conn.execute(
        "SELECT COUNT(*) FROM restaurants WHERE city_id = ?", (city_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE cities SET restaurant_count = ? WHERE id = ?", (count, city_id)
    )
    conn.commit()


# ── restaurant helpers ────────────────────────────────────────────────────────

def upsert_restaurant(conn, city_id, name, slug, url, address, phone, delivery, info):
    existing = conn.execute(
        "SELECT id FROM restaurants WHERE city_id = ? AND slug = ?",
        (city_id, slug)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE restaurants SET name=?, url=?, address=?, phone=?, delivery=?, info=? WHERE id=?",
            (name, url, address, phone, int(delivery), info, existing[0])
        )
        conn.commit()
        return existing[0]
    cur = conn.execute(
        "INSERT INTO restaurants (city_id, name, slug, url, address, phone, delivery, info) VALUES (?,?,?,?,?,?,?,?)",
        (city_id, name, slug, url, address, phone, int(delivery), info)
    )
    conn.commit()
    return cur.lastrowid


def update_restaurant_active_days(conn, restaurant_id, weekday: int, last_seen: str):
    """Add weekday (0-4) to the restaurant's active_days set and update last_seen."""
    row = conn.execute(
        "SELECT active_days FROM restaurants WHERE id = ?", (restaurant_id,)
    ).fetchone()
    if not row:
        return
    try:
        days = set(json.loads(row[0] or "[]"))
    except (json.JSONDecodeError, TypeError):
        days = set()
    days.add(weekday)
    conn.execute(
        "UPDATE restaurants SET active_days = ?, last_seen = ? WHERE id = ?",
        (json.dumps(sorted(days)), last_seen, restaurant_id)
    )
    conn.commit()


# ── scrape run helpers ────────────────────────────────────────────────────────

def upsert_scrape_run(conn, city_id, source, scraped_at, date):
    """
    Insert or replace a scrape run for (city, source, date).
    Deletes existing menu_items for the old run first to avoid duplicates.
    Returns the new run_id.
    """
    existing = conn.execute(
        "SELECT id FROM scrape_runs WHERE city_id=? AND source=? AND date=?",
        (city_id, source, date)
    ).fetchone()

    if existing:
        old_id = existing[0]
        conn.execute("DELETE FROM menu_items WHERE scrape_run_id=?", (old_id,))
        conn.execute("DELETE FROM scrape_runs WHERE id=?", (old_id,))
        conn.commit()

    cur = conn.execute(
        "INSERT INTO scrape_runs (city_id, source, scraped_at, date) VALUES (?,?,?,?)",
        (city_id, source, scraped_at, date)
    )
    conn.commit()
    return cur.lastrowid


def latest_run_id_for(conn, city_slug: str, on_date: str, source: str = None):
    """
    Return the most recent scrape run id for a city+date.
    If on_date has no data, falls back to the most recent available run
    for that city (so weekends and sparse days still return something).
    """
    params = [city_slug, on_date]
    q = """
        SELECT sr.id FROM scrape_runs sr
        JOIN cities c ON c.id = sr.city_id
        WHERE c.slug = ? AND sr.date = ?
    """
    if source:
        q += " AND sr.source = ?"
        params.append(source)
    q += " ORDER BY sr.id DESC LIMIT 1"
    row = conn.execute(q, params).fetchone()
    if row:
        return row[0], on_date

    # Fallback: most recent run for this city regardless of date
    fallback_params = [city_slug]
    fq = """
        SELECT sr.id, sr.date FROM scrape_runs sr
        JOIN cities c ON c.id = sr.city_id
        WHERE c.slug = ?
    """
    if source:
        fq += " AND sr.source = ?"
        fallback_params.append(source)
    fq += " ORDER BY sr.date DESC, sr.id DESC LIMIT 1"
    row = conn.execute(fq, fallback_params).fetchone()
    if row:
        return row[0], row[1]

    return None, None