# main.py - Scraper for namenu.sk, extracting restaurant and menu data into SQLite database
import requests
from bs4 import BeautifulSoup
import json
import re
import sqlite3
import hashlib
import os
from datetime import datetime, date
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


DB_PATH = os.environ.get("NAMENU_DB", "namenu.db")

CITIES = [
    {"name": "Levice",           "slug": "levice",          "url": "https://lv.namenu.sk/"},
    {"name": "Nové Zámky",       "slug": "nove-zamky",      "url": "https://namenu.sk/nove_zamky/"},
    {"name": "Zlaté Moravce",    "slug": "zlate-moravce",   "url": "https://namenu.sk/zlate_moravce/"},
    {"name": "Žarnovica",        "slug": "zarnovica",       "url": "https://namenu.sk/zarnovica/"},
    {"name": "Zvolen",           "slug": "zvolen",          "url": "https://namenu.sk/zvolen/"},
    {"name": "Žiar nad Hronom",  "slug": "ziar-nad-hronom", "url": "https://namenu.sk/ziar_nad_hronom/"},
    {"name": "Banská Štiavnica", "slug": "banska-stiavnica","url": "https://namenu.sk/banska_stiavnica/"},
]

HEADERS = {"User-Agent": "namenu-scraper/1.0 (personal project)"}

# ── classification ────────────────────────────────────────────────────────────

SOUP_KEYWORDS = [
    "polievka","vývar","kapustnica","gulášová","šošovicová","fazuľová",
    "paradajková","hrášková","frankfurtská","hŕstková","hokaido",
    "zemiaková pol","zeleninová pol","cícerovo-kel"
]
DESSERT_KEYWORDS = [
    "dezert","lievance","lievančeky","buchty","šišky","nákyp","chia",
    "tiramisu","perky naplnené","šúlance","orechové pečené rožky","palacinky"
]

def classify_type(text):
    t = text.lower()
    if re.match(r'^p\d?[.:\s]|^polievka\s*[č\d]', t): return "soup"
    if re.match(r'^dezert', t):                        return "dessert"
    for kw in SOUP_KEYWORDS:
        if kw in t: return "soup"
    for kw in DESSERT_KEYWORDS:
        if kw in t: return "dessert"
    return "main"

# ── field extractors ──────────────────────────────────────────────────────────

def extract_price(text):
    patterns = [
        r'(\d+[.,]\d{2})\s*(?:€|EUR|eur|Eur)(?!\w)',
        r'(?:€)\s*(\d+[.,]\d{2})',
        r'(\d+),-\s*(?:€|EUR)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return float(m.group(1).replace(",", "."))
    m = re.search(r'(?:^|\s)(\d{1,2}[.,]\d{2})\s*$', text)
    if m:
        val = float(m.group(1).replace(',', '.'))
        if 3.0 <= val <= 25.0:
            return val
    return None

def extract_allergens(text):
    m = re.search(r'Alerg[eé]ny[:\s]+[-–]?\s*([0-9][0-9,\s]*)', text, re.IGNORECASE)
    if m:
        return [int(n) for n in re.findall(r'\d+', m.group(1)) if 1 <= int(n) <= 14]
    m = re.search(r'[(/]([0-9][0-9,\s]*)[)/]', text)
    if m and not re.search(r'[a-zA-ZáčďéíľňóšťúýžÁČĎÉÍĽŇÓŠŤÚÝŽ/]', m.group(1)):
        nums = [int(n) for n in re.findall(r'\d+', m.group(1)) if 1 <= int(n) <= 14]
        if nums: return nums
    m = re.search(r',\s*A:\s*([\d,\s]+)$', text)
    if m:
        nums = [int(n) for n in re.findall(r'\d+', m.group(1)) if 1 <= int(n) <= 14]
        if nums: return nums
    m = re.search(r'•[^|]*?\s([\d,]+)\s*\|', text)
    if m:
        nums = [int(n) for n in re.findall(r'\d+', m.group(1)) if 1 <= int(n) <= 14]
        if nums: return nums
    m = re.search(r'\s((?:\d{1,2},)+\d{1,2})\s+\(\d', text)
    if m:
        nums = [int(n) for n in re.findall(r'\d+', m.group(1)) if 1 <= int(n) <= 14]
        if nums and all(1 <= n <= 14 for n in nums): return nums
    m = re.search(r'(?:^|\s)((?:\d{1,2},)+\d{1,2})(?:\s+\d{1,2}[.,]\d{2}\s*(?:\w+)?)?\s*$', text)
    if m:
        nums = [int(n) for n in re.findall(r'\d+', m.group(1))]
        if nums and all(1 <= n <= 14 for n in nums):
            return nums
    m = re.search(r'(?<![,\d])\s+(\d{1,2})\s*$', text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 14: return [n]
    m = re.search(r'/\s*(\d{1,2})\s*$', text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 14: return [n]
    return []

def extract_weight(text):
    t = re.sub(r'^\w{1,2}[.:]\s*', '', text.strip())
    t = re.sub(r'(\d)gr\.', r'\1g', t)
    m = re.search(r'(\d+(?:[./]\d+)*\s*(?:g|kg|dcl|ml))(?!\s*(?:EUR|€|eur))', t, re.IGNORECASE)
    if m: return m.group(1).strip()
    m = re.search(r'(0[.,]\d+\s*l(?:iter)?)\b', t, re.IGNORECASE)
    return m.group(1).strip() if m else None

def clean_name(text):
    t = text
    t = re.sub(r'^(?:BIZNIS\s+MENU|XXL|Menu\s+\w{1,2}|Polievka\s*(?:č\.)?\s*\d*|Dezert|Vegán)[.:\s]+', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^[A-Z]\s+(?=\d)', '', t)
    t = re.sub(r'^[A-Za-z0-9]\.\s*', '', t)
    t = re.sub(r'^\w\s*[.:)]\s+', '', t)
    t = re.sub(r'^\w{1,2}[.:)\s]\s+', '', t)
    t = re.sub(r'(\d)gr\.', r'\1g', t)
    t = re.sub(r'\d+(?:[.,/]\d+)*\s*(?:g|kg|l|ml|dcl)\s*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^r\.\s*', '', t)
    t = re.sub(r'^\w\s*:\s*', '', t)
    t = re.sub(r'\s*[-–]\s*(?:€\s*)?\d+[.,\-]\d*\s*(?:€|EUR|eur|Eur)?', '', t)
    t = re.sub(r'€\s*\d+[.,]\d{2}', '', t)
    t = re.sub(r'(?:€\s*)?\d+[.,]\d{2}\s*(?:€|EUR|eur|Eur)', '', t)
    t = re.sub(r'\d+,-\s*(?:€|EUR)', '', t)
    t = re.sub(r'\s*\([^)]*[a-zA-ZáčďéíľňóšťúýžÁČĎÉÍĽŇÓŠŤÚÝŽ][^)]*\)', '', t)
    t = re.sub(r'Alerg[eé]ny[:\s]*[-–]?\s*[0-9,\s]+', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[(/]\s*[0-9][0-9,\s]*\s*[)/]', '', t)
    t = re.sub(r'\s*[-–|•]\s*\d+\s*kcal.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\d+\s*(?:kcal|cal)\b.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bB:\d+.*', '', t)
    t = re.sub(r'^[●•]\s*', '', t)
    t = re.sub(r'[()]+', '', t)
    t = re.sub(r'^\d+\s+ks\s+', '', t)
    t = re.sub(r'\s*cena\s+bez\s+polievky.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*•.*$', '', t)
    t = re.sub(r'\s*\(\d[/\d,\s]*kačice[^)]*\)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+(?:\d{1,2},)+\d{1,2}\s+\(', ' (', t)
    t = re.sub(r'\s+\d{1,2}\s*$', '', t)
    t = re.sub(r',\s*A:\s*[\d,\s]+$', '', t)
    t = re.sub(r'\s+(?:\d{1,2},)+\d{1,2}(?:\s+\d{1,2}[.,]\d{2}(?:\s*(?:€|EUR|eur|Eur))?)?\s*$', '', t)
    t = re.sub(r'\s+\d{1,2}[.,]\d{2}\s*$', '', t)
    t = re.sub(r'\s*/\s*\d{1,2}\s*$', '', t)
    t = re.sub(r'\s+(?:\d{1,2},)+\d{1,2}\s*$', '', t)
    t = re.sub(r'\s+\d{1,2}\s*$', '', t)
    return re.sub(r'\s+', ' ', t).strip(" |–-,/.")

def is_item_start(text):
    t = text.strip()
    if not t: return False
    if re.match(r'^[BTSVL]:\d', t): return False
    if re.match(r'^\(?\.?\d+\s*(?:kcal|cal)\b', t, re.IGNORECASE): return False
    patterns = [
        r'^\d+[.:]\s*\S',
        r'^\w[.:]\s',
        r'^[A-Za-z]\.\S',
        r'^\w\s*:\s*\w',
        r'^\w\)\s',
        r'^(?:Menu|MENU)\s+\w',
        r'^(?:BIZNIS\s+MENU|XXL)[.:\s]',
        r'^(?:Dezert|Vegán)[.:\s]',
        r'^P\d?[.:\s]',
        r'^(?:Polievka\s*(?:č\.)?\s*\d*)[.:\s]',
        r'^\d+[.,]\d+\s*[lLgGdD]',
        r'^\d+\s*[gG]\s',
        r'^\d+(?:/\d+)+\s*[gG]\s',
        r'^[A-Z]\s+\d',
    ]
    for pat in patterns:
        if re.match(pat, t, re.IGNORECASE): return True
    for kw in SOUP_KEYWORDS:
        if t.lower().startswith(kw): return True
    if re.match(r'^[A-ZÁČĎÉÍĽŇÓŠŤÚÝŽ]{3,}', t): return True
    return False

# ── line grouping ─────────────────────────────────────────────────────────────

def group_lines_into_items(lines):
    items   = []
    current = None
    for line in lines:
        line = line.replace('\u00a0', ' ').strip()
        if not line:
            continue
        if is_item_start(line):
            if current:
                items.append(current)
            current = {"main_line": line, "extra": []}
        elif current is not None:
            current["extra"].append(line)
        else:
            current = {"main_line": line, "extra": []}
    if current:
        items.append(current)
    return items

# ── item parsing ──────────────────────────────────────────────────────────────

def parse_macro_line(line):
    nutrition = {}
    for key, field in [('B','protein_g'),('T','fat_g'),('S','carbs_g'),('Vl','fiber_g'),('E','kcal')]:
        m = re.search(key + r':(\d+(?:[.,]\d+)?)\s*(?:g|kcal)?', line)
        if m: nutrition[field] = float(m.group(1).replace(',','.'))
    return nutrition if nutrition else None

def parse_item(group):
    main  = group["main_line"]
    extra = group["extra"]
    all_text = main + " " + " ".join(extra)

    allergens      = extract_allergens(all_text)
    price          = extract_price(all_text)
    weight         = extract_weight(main)
    name           = clean_name(main)
    kind           = classify_type(main)
    main_kcal      = re.search(r'(\d+)\s*(?:kcal|cal)\b', main, re.IGNORECASE)
    main_nutrition = {'kcal': int(main_kcal.group(1))} if main_kcal else {}

    desc_parts = []
    nutrition  = {}
    for line in extra:
        line = line.replace('\u00a0', ' ').strip()
        if re.match(r'^[Aa]lerg[eé]ny', line): continue
        if re.match(r'^[BTSVL]:\d', line):
            n = parse_macro_line(line)
            if n: nutrition.update(n)
            continue
        km = re.match(r'^\(?.*?\)?\s*(\d+)\s*(?:kcal|cal)\b', line, re.IGNORECASE)
        if km:
            nutrition['kcal'] = int(km.group(1))
            continue
        if re.match(r'^/', line): continue
        if len(line) < 5: continue
        desc_parts.append(line)

    description = " ".join(desc_parts).strip() if desc_parts else None
    if description:
        description = re.sub(r'Alerg[eé]ny[:\s]*[-–]?\s*[0-9,\s]+', '', description, flags=re.IGNORECASE)
        description = re.sub(r'[(/]\s*[0-9][0-9,\s]*\s*[)/]', '', description)
        description = re.sub(r'(?:€\s*)?\d+[.,]\d{2}\s*(?:€|EUR|eur|Eur)', '', description)
        description = re.sub(r'\s+', ' ', description).strip(" |–-,/.")
        if len(description) < 5:
            description = None

    merged_nutrition = {**main_nutrition, **nutrition}
    return {
        "type":        kind,
        "name":        name,
        "description": description,
        "weight":      weight,
        "price_eur":   price,
        "allergens":   allergens,
        "nutrition":   merged_nutrition if merged_nutrition else None,
        "raw":         main
    }

# ── database ──────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cities (
            id    INTEGER PRIMARY KEY,
            name  TEXT NOT NULL,
            slug  TEXT NOT NULL UNIQUE,
            url   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS restaurants (
            id        INTEGER PRIMARY KEY,
            city_id   INTEGER NOT NULL REFERENCES cities(id),
            name      TEXT NOT NULL,
            slug      TEXT NOT NULL,
            url       TEXT,
            address   TEXT,
            phone     TEXT,
            delivery  INTEGER NOT NULL DEFAULT 0,
            info      TEXT,
            UNIQUE(city_id, slug)
        );

        CREATE TABLE IF NOT EXISTS scrape_runs (
            id         INTEGER PRIMARY KEY,
            city_id    INTEGER NOT NULL REFERENCES cities(id),
            scraped_at TEXT NOT NULL,
            day        TEXT NOT NULL,
            date       TEXT NOT NULL
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
            nutrition     TEXT,
            raw           TEXT
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id         INTEGER PRIMARY KEY,
            key_hash   TEXT NOT NULL UNIQUE,
            label      TEXT,
            created_at TEXT NOT NULL,
            last_used  TEXT,
            active     INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_menu_items_run   ON menu_items(scrape_run_id);
        CREATE INDEX IF NOT EXISTS idx_menu_items_rest  ON menu_items(restaurant_id);
        CREATE INDEX IF NOT EXISTS idx_menu_items_type  ON menu_items(type);
        CREATE INDEX IF NOT EXISTS idx_scrape_runs_date ON scrape_runs(date);
        CREATE INDEX IF NOT EXISTS idx_restaurants_city ON restaurants(city_id);
        CREATE INDEX IF NOT EXISTS idx_restaurants_slug ON restaurants(city_id, slug);
    """)
    conn.commit()

def get_or_create_city(conn, name, slug, url):
    row = conn.execute("SELECT id FROM cities WHERE slug = ?", (slug,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO cities (name, slug, url) VALUES (?, ?, ?)",
        (name, slug, url)
    )
    conn.commit()
    return cur.lastrowid

def upsert_restaurant(conn, city_id, name, slug, url, address, phone, delivery, info):
    existing = conn.execute(
        "SELECT id FROM restaurants WHERE city_id = ? AND slug = ?",
        (city_id, slug)
    ).fetchone()
    if existing:
        conn.execute("""
            UPDATE restaurants
            SET name=?, url=?, address=?, phone=?, delivery=?, info=?
            WHERE id=?
        """, (name, url, address, phone, int(delivery), info, existing[0]))
        conn.commit()
        return existing[0]
    cur = conn.execute("""
        INSERT INTO restaurants (city_id, name, slug, url, address, phone, delivery, info)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (city_id, name, slug, url, address, phone, int(delivery), info))
    conn.commit()
    return cur.lastrowid

def make_slug(name):
    slug = name.lower()
    for src, dst in [
        ('áä','a'),('čć','c'),('ď','d'),('éě','e'),('í','i'),
        ('ľĺ','l'),('ň','n'),('óô','o'),('š','s'),('ť','t'),
        ('úů','u'),('ý','y'),('ž','z'),
    ]:
        for ch in src:
            slug = slug.replace(ch, dst)
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_-]+', '-', slug)
    return slug.strip('-')

# ── scraper ───────────────────────────────────────────────────────────────────

def scrape():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    today       = date.today().isoformat()
    now         = datetime.now().isoformat()
    total_items = 0
    last_run_id = None

    for city_def in CITIES:
        print(f"\n── {city_def['name']} ──")
        try:
            response = requests.get(city_def["url"], headers=HEADERS, timeout=15)
        except Exception as e:
            print(f"  skipped ({e})")
            continue

        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        title_tag = soup.find("title")
        day_label = title_tag.text.strip().split("|")[0].strip() if title_tag else ""

        city_id = get_or_create_city(conn, city_def["name"], city_def["slug"], city_def["url"])

        # ── check if this city actually has menu data ──────────────────────
        # Dead cities return a page with no h2 restaurant blocks at all
        restaurants_on_page = soup.find_all("h2")
        if not restaurants_on_page:
            print(f"  skipped (no restaurant data — likely Coming Soon page)")
            continue

        cur = conn.execute(
            "INSERT INTO scrape_runs (city_id, scraped_at, day, date) VALUES (?, ?, ?, ?)",
            (city_id, now, day_label, today)
        )
        conn.commit()
        run_id      = cur.lastrowid
        last_run_id = run_id
        city_items  = 0

        # ── scrape restaurants for THIS city ──────────────────────────────
        for h2 in restaurants_on_page:
            name_tag = h2.find("a")
            if not name_tag:
                continue

            name = name_tag.text.strip()
            url  = name_tag.get("href", "")
            slug = make_slug(name)

            siblings = []
            node = h2.find_next_sibling()
            while node and node.name != "h2":
                siblings.append(node)
                node = node.find_next_sibling()

            full_text = " ".join(s.get_text(" ", strip=True) for s in siblings)

            phone_m  = re.search(r'[\+0][\d\s/]{7,}', full_text)
            phone    = phone_m.group(0).strip() if phone_m else ""
            delivery = any(
                "delivery_green" in img.get("src", "")
                for s in siblings for img in s.find_all("img")
            )
            address = ""
            for s in siblings:
                pin = s.find("img", src=lambda x: x and "icon_pin" in x)
                if pin:
                    address = pin.get("title", "").strip()
                    break

            info_p = h2.find_next_sibling("p")
            info   = info_p.get_text(" ", strip=True) if info_p else ""

            menu_price  = None
            header_text = " ".join(s.get_text(" ", strip=True) for s in siblings[:4])
            for mp_pat in [
                r'(?:od|from|za)\s*(\d+[.,]\d{2})\s*(?:€|EUR|eur)',
                r'^\s*(\d+[.,]\d{2})\s*(?:€|EUR)',
                r'(\d+[.,]\d{2})\s*(?:€|EUR)',
            ]:
                mp = re.search(mp_pat, header_text, re.IGNORECASE)
                if mp:
                    menu_price = float(mp.group(1).replace(',', '.'))
                    break

            rest_id = upsert_restaurant(
                conn, city_id, name, slug, url,
                address, phone, delivery, info
            )

            raw_lines = []
            for s in siblings:
                table = s if s.name == "table" else s.find("table")
                if not table:
                    continue
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        lines = [l.strip() for l in cells[1].get_text("\n", strip=True).split("\n") if l.strip()]
                        raw_lines.extend(lines)

            groups = group_lines_into_items(raw_lines)
            items  = [parse_item(g) for g in groups]

            rows = [
                (
                    rest_id, run_id,
                    item["type"], item["name"], item["description"],
                    item["weight"], item["price_eur"], menu_price,
                    json.dumps(item["allergens"], ensure_ascii=False) if item["allergens"] else "[]",
                    json.dumps(item["nutrition"],  ensure_ascii=False) if item["nutrition"]  else None,
                    item["raw"],
                )
                for item in items
            ]

            conn.executemany("""
                INSERT INTO menu_items
                    (restaurant_id, scrape_run_id, type, name, description,
                     weight, price_eur, menu_price, allergens, nutrition, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            city_items  += len(items)
            total_items += len(items)
            print(f"  {name}: {len(items)} items")

        # ── discard run if nothing was scraped ─────────────────────────────
        if city_items == 0:
            conn.execute("DELETE FROM scrape_runs WHERE id = ?", (run_id,))
            conn.commit()
            last_run_id = None
            print(f"  (no items found — run discarded)")
        else:
            print(f"  → {city_items} items total for {city_def['name']}")

    conn.close()
    print(f"\nDone: {total_items} total items -> {DB_PATH}  (last_run_id={last_run_id}, date={today})")

# ── api key management ────────────────────────────────────────────────────────

def add_api_key(label):
    import secrets
    key  = secrets.token_urlsafe(32)
    h    = hashlib.sha256(key.encode()).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.execute(
        "INSERT INTO api_keys (key_hash, label, created_at) VALUES (?, ?, ?)",
        (h, label, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    print(f"API key created for '{label}':")
    print(f"  {key}")
    print("Save this — it won't be shown again.")

def list_api_keys():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    rows = conn.execute(
        "SELECT id, label, created_at, last_used, active FROM api_keys ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        print("No API keys yet. Use --add-key to create one.")
        return
    print(f"{'ID':>4}  {'Label':<25}  {'Created':<20}  {'Last used':<20}  Active")
    print("-" * 80)
    for r in rows:
        print(f"{r[0]:>4}  {(r[1] or ''):.<25}  {r[2][:19]:<20}  {(r[3] or 'never')[:19]:<20}  {'yes' if r[4] else 'no'}")

def revoke_api_key(key_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    print(f"Key {key_id} revoked.")

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "--add-key":
        add_api_key(args[1] if len(args) > 1 else "default")
    elif args and args[0] == "--list-keys":
        list_api_keys()
    elif args and args[0] == "--revoke-key":
        revoke_api_key(int(args[1]))
    else:
        scrape()