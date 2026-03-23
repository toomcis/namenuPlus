# scrapers/namenu.scrape.py
# Scrapes lv.namenu.sk (and other namenu cities) for the full current week.
# Writes flat macros (kcal, protein_g etc.), auto-tags each dish, and
# updates active_days on each restaurant after a successful scrape.
#
# Tagging strategy (hybrid):
#   1. If ml/tagger.py exists and a trained model is present, use ml.tagger.predict()
#      which returns ML tags merged with rule-based tags.
#   2. If no model exists yet (first run, fresh install), falls back to the
#      local auto_tag() rule engine — identical output to before.
#
# Usage:
#   python -X utf8 scrapers/namenu.scrape.py           # scrape whole week
#   python -X utf8 scrapers/namenu.scrape.py --today   # scrape today only
#   python -X utf8 scrapers/namenu.scrape.py --day pondelok

import sys
import os
import io
import re
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.db import (
    connect, init_db,
    get_or_create_city, upsert_restaurant, upsert_scrape_run,
    update_restaurant_active_days, update_city_restaurant_count,
)

# ── ML tagger (optional — falls back gracefully if not available) ──────────────
# We try to import predict() from ml.tagger.  If the module is missing or the
# model hasn't been trained yet, _tag_dish() falls back to the local auto_tag().

_ml_predict = None

try:
    from ml.tagger import predict as _ml_predict_fn
    _ml_predict = _ml_predict_fn
    print("[tagger] ML model loaded — using ml.tagger.predict (hybrid)", flush=True)
except Exception as _ml_import_err:
    print(f"[tagger] ML model not available ({_ml_import_err}), using rule-based auto_tag", flush=True)


SOURCE  = "namenu"
HEADERS = {"User-Agent": "namenu-scraper/1.0 (personal project)"}

# ── cities ────────────────────────────────────────────────────────────────────
# Coordinates are approximate city centres — used for map display only.
# Only Levice actually has data from lv.namenu.sk right now.

CITIES = [
    {"name": "Levice",           "slug": "levice",          "url": "https://lv.namenu.sk/",                      "lat": 48.2139, "lon": 18.6094},
    {"name": "Nové Zámky",       "slug": "nove-zamky",      "url": "https://namenu.sk/nove_zamky/",              "lat": 47.9853, "lon": 18.1637},
    {"name": "Zlaté Moravce",    "slug": "zlate-moravce",   "url": "https://namenu.sk/zlate_moravce/",           "lat": 48.3853, "lon": 18.3961},
    {"name": "Žarnovica",        "slug": "zarnovica",       "url": "https://namenu.sk/zarnovica/",               "lat": 48.4872, "lon": 18.7157},
    {"name": "Zvolen",           "slug": "zvolen",          "url": "https://namenu.sk/zvolen/",                  "lat": 48.5748, "lon": 19.1223},
    {"name": "Žiar nad Hronom",  "slug": "ziar-nad-hronom", "url": "https://namenu.sk/ziar_nad_hronom/",         "lat": 48.5891, "lon": 18.8538},
    {"name": "Banská Štiavnica", "slug": "banska-stiavnica","url": "https://namenu.sk/banska_stiavnica/",        "lat": 48.4592, "lon": 18.8994},
]

# ── day mapping ───────────────────────────────────────────────────────────────

DAYS = [
    {"slug": "pondelok", "weekday": 0},
    {"slug": "utorok",   "weekday": 1},
    {"slug": "streda",   "weekday": 2},
    {"slug": "stvrtok",  "weekday": 3},
    {"slug": "piatok",   "weekday": 4},
]


def day_url(city_base_url, day_slug):
    if "lv.namenu.sk" in city_base_url:
        return f"https://lv.namenu.sk/menu_den/menu_{day_slug}/"
    base = city_base_url.rstrip("/")
    return f"{base}/menu_den/menu_{day_slug}/"


def week_date_for(weekday: int) -> date:
    today  = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(days=weekday)


# ── tag rules (rule-based fallback — also used by ml/tagger.py) ───────────────
# These run when no trained model is available, and as a safety net inside
# ml.tagger.predict() for the hybrid merge.

MEAT_KEYWORDS = [
    "mäso", "mäsov", "rezen", "rezeň", "bravčov", "bravčové",
    "hovädz", "hovädzí", "hovädzieho", "jahňa", "teľac",
    "šunka", "slanina", "bôčik", "reberc", "biftek", "roastbeef",
    "mleté", "mletého", "sekaná", "pečené mäso",
    "kura", "kurací", "kuracích", "kurča", "kurčaťa",
    "morka", "morčac", "kačic", "králik",
]

FISH_KEYWORDS = [
    "ryba", "rybacia", "losos", "treska", "pstruh", "kapor",
    "filé", "tuniak", "sardina", "platesa", "tilapia",
]

TAG_RULES: dict[str, list[str]] = {
    "chicken":  ["kura", "kurací", "kuracích", "kurča", "kurčaťa"],
    "pork":     ["bravčov", "bravčové", "šunka", "slanina", "bôčik", "reberc"],
    "beef":     ["hovädz", "hovädzí", "hovädzieho", "biftek", "roastbeef"],
    "fish":     ["ryba", "rybacia", "losos", "treska", "pstruh", "kapor",
                 "filé", "tuniak", "sardina", "platesa", "tilapia"],
    "fried":    ["vyprážan", "smaž", "smažen", "fritovan"],
    "grilled":  ["grilovan", "na grile", "grill"],
    "baked":    ["pečen", "zapékan", "zapečen", "v rúre"],
    "steamed":  ["dusené", "dusen", "varené v pare"],
    "pasta":    ["halušky", "cestoviny", "špagety", "tagliatelle",
                 "penne", "fusilli", "lasagne", "rezance", "noky"],
    "rice":     ["ryža", "rizoto", "pilaf"],
    "salad":    ["šalát", "salát"],
    "soup":     ["polievka", "vývar", "gulášová", "kapustnica",
                 "šošovicová", "fazuľová", "paradajková", "zemiaková pol"],
    "burger":   ["burger", "hamburger"],
    "sandwich": ["sendvič", "toast", "bagel", "wrap"],
    "pizza":    ["pizza", "pizz"],
    "asian":    ["čínsk", "japonsk", "thajsk", "wok", "sushi",
                 "nudle", "ramen", "pho"],
    "dessert":  ["dezert", "lievance", "lievančeky", "buchty", "šišky",
                 "nákyp", "tiramisu", "palacinky", "koláč", "torta"],
    "dairy":    ["bryndza", "syr", "syrov", "smotana", "maslo",
                 "jogurt", "mozzarella", "parmezan", "ricotta"],
    "egg":      ["vajce", "vajíčko", "omeleta", "praženica"],
    "spicy":    ["pálivý", "pálivé", "chili", "jalapeño", "korenený"],
    "sweet":    ["sladký", "med", "karamel", "čokolád", "vanilk", "ovocn"],
    "healthy":  ["celozrnn", "nízkotučn", "light", "fit", "bezlepkov"],
    "vegan":    ["tofu", "hummus", "seitan", "tempeh"],
}

VEGETARIAN_ONLY_KEYWORDS = [
    "špenát", "zelenin", "zeleninov", "bryndza",
    "tofu", "hummus", "cícer", "šošovica", "fazuľa",
    "hrášok", "brokolica", "karfiol", "vegán", "vegetarián",
]


def auto_tag(dish_name: str, dish_type: str, description: str = "") -> list[str]:
    """Rule-based tagger — no model required, always available."""
    text = ((dish_name or "") + " " + (description or "")).lower()
    tags: set[str] = set()

    if dish_type == "soup":
        tags.add("soup")

    has_meat = any(kw in text for kw in MEAT_KEYWORDS)
    has_fish = any(kw in text for kw in FISH_KEYWORDS)

    if has_meat:
        tags.add("meat")
    if has_fish:
        tags.add("fish")

    for tag, keywords in TAG_RULES.items():
        for kw in keywords:
            if kw in text:
                tags.add(tag)
                break

    if not has_meat and not has_fish:
        for kw in VEGETARIAN_ONLY_KEYWORDS:
            if kw in text:
                tags.add("vegetarian")
                break

    return sorted(tags)


def _tag_dish(name: str, dish_type: str, description: str = "") -> list[str]:
    """
    Tag a dish using the ML model if available, otherwise fall back to rules.
    This is the single call site used by parse_item() — swap logic here only.
    """
    if _ml_predict is not None:
        try:
            return _ml_predict(name, dish_type, description)
        except Exception as e:
            # Model error on a specific item — fall back to rules for this item only
            print(f"    [tagger] ML error for '{name}': {e}, using rules", flush=True)
    return auto_tag(name, dish_type, description)


# ── classification ────────────────────────────────────────────────────────────

SOUP_KEYWORDS = [
    "polievka", "vývar", "kapustnica", "gulášová", "šošovicová", "fazuľová",
    "paradajková", "hrášková", "frankfurtská", "hŕstková", "hokaido",
    "zemiaková pol", "zeleninová pol", "cícerovo-kel",
]
DESSERT_KEYWORDS = [
    "dezert", "lievance", "lievančeky", "buchty", "šišky", "nákyp", "chia",
    "tiramisu", "perky naplnené", "šúlance", "orechové pečené rožky", "palacinky",
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
    for pat in [
        r'(\d+[.,]\d{2})\s*(?:€|EUR|eur|Eur)(?!\w)',
        r'(?:€)\s*(\d+[.,]\d{2})',
        r'(\d+),-\s*(?:€|EUR)',
    ]:
        m = re.search(pat, text)
        if m: return float(m.group(1).replace(",", "."))
    m = re.search(r'(?:^|\s)(\d{1,2}[.,]\d{2})\s*$', text)
    if m:
        val = float(m.group(1).replace(',', '.'))
        if 3.0 <= val <= 25.0: return val
    return None


def extract_allergens(text):
    m = re.search(r'Alerg[eé]ny[:\s]+[-–]?\s*([0-9][0-9,\s]*)', text, re.IGNORECASE)
    if m: return [int(n) for n in re.findall(r'\d+', m.group(1)) if 1 <= int(n) <= 14]
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
        if nums and all(1 <= n <= 14 for n in nums): return nums
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
        r'^\d+[.:]\s*\S', r'^\w[.:]\s', r'^[A-Za-z]\.\S', r'^\w\s*:\s*\w',
        r'^\w\)\s', r'^(?:Menu|MENU)\s+\w', r'^(?:BIZNIS\s+MENU|XXL)[.:\s]',
        r'^(?:Dezert|Vegán)[.:\s]', r'^P\d?[.:\s]',
        r'^(?:Polievka\s*(?:č\.)?\s*\d*)[.:\s]',
        r'^\d+[.,]\d+\s*[lLgGdD]', r'^\d+\s*[gG]\s',
        r'^\d+(?:/\d+)+\s*[gG]\s', r'^[A-Z]\s+\d',
    ]
    for pat in patterns:
        if re.match(pat, t, re.IGNORECASE): return True
    for kw in SOUP_KEYWORDS:
        if t.lower().startswith(kw): return True
    if re.match(r'^[A-ZÁČĎÉÍĽŇÓŠŤÚÝŽ]{3,}', t): return True
    return False


def group_lines_into_items(lines):
    items, current = [], None
    for line in lines:
        line = line.replace('\u00a0', ' ').strip()
        if not line: continue
        if is_item_start(line):
            if current: items.append(current)
            current = {"main_line": line, "extra": []}
        elif current is not None:
            current["extra"].append(line)
        else:
            current = {"main_line": line, "extra": []}
    if current: items.append(current)
    return items


def parse_macro_line(line):
    nutrition = {}
    for key, field in [('B', 'protein_g'), ('T', 'fat_g'), ('S', 'carbs_g'), ('Vl', 'fiber_g'), ('E', 'kcal')]:
        m = re.search(key + r':(\d+(?:[.,]\d+)?)\s*(?:g|kcal)?', line)
        if m: nutrition[field] = float(m.group(1).replace(',', '.'))
    return nutrition if nutrition else None


def parse_item(group):
    """Parse a raw line group into a structured dish dict with flat macros and tags."""
    main, extra = group["main_line"], group["extra"]
    all_text = main + " " + " ".join(extra)

    allergens = extract_allergens(all_text)
    price     = extract_price(all_text)
    weight    = extract_weight(main)
    name      = clean_name(main)
    kind      = classify_type(main)

    # ── macros ────────────────────────────────────────────────────────────────
    main_kcal_m = re.search(r'(\d+)\s*(?:kcal|cal)\b', main, re.IGNORECASE)
    macros: dict = {}
    if main_kcal_m:
        macros["kcal"] = int(main_kcal_m.group(1))

    desc_parts = []
    for line in extra:
        line = line.replace('\u00a0', ' ').strip()
        if re.match(r'^[Aa]lerg[eé]ny', line): continue
        if re.match(r'^[BTSVL]:\d', line):
            n = parse_macro_line(line)
            if n: macros.update(n)
            continue
        km = re.match(r'^\(?.*?\)?\s*(\d+)\s*(?:kcal|cal)\b', line, re.IGNORECASE)
        if km:
            macros["kcal"] = int(km.group(1))
            continue
        if re.match(r'^/', line) or len(line) < 5: continue
        desc_parts.append(line)

    description = " ".join(desc_parts).strip() or None
    if description:
        description = re.sub(r'Alerg[eé]ny[:\s]*[-–]?\s*[0-9,\s]+', '', description, flags=re.IGNORECASE)
        description = re.sub(r'[(/]\s*[0-9][0-9,\s]*\s*[)/]', '', description)
        description = re.sub(r'(?:€\s*)?\d+[.,]\d{2}\s*(?:€|EUR|eur|Eur)', '', description)
        description = re.sub(r'\s+', ' ', description).strip(" |–-,/.")
        if len(description) < 5: description = None

    # ── tagging — uses ML if available, rules as fallback ─────────────────────
    tags = _tag_dish(name, kind, description or "")

    return {
        "type":        kind,
        "name":        name,
        "description": description,
        "weight":      weight,
        "price_eur":   price,
        "allergens":   allergens,
        "kcal":        macros.get("kcal"),
        "protein_g":   macros.get("protein_g"),
        "fat_g":       macros.get("fat_g"),
        "carbs_g":     macros.get("carbs_g"),
        "fiber_g":     macros.get("fiber_g"),
        "tags":        tags,
        "raw":         main,
    }


def make_slug(name):
    slug = name.lower()
    for src, dst in [('áä', 'a'), ('čć', 'c'), ('ď', 'd'), ('éě', 'e'), ('í', 'i'),
                     ('ľĺ', 'l'), ('ň', 'n'), ('óô', 'o'), ('š', 's'), ('ť', 't'),
                     ('úů', 'u'), ('ý', 'y'), ('ž', 'z')]:
        for ch in src: slug = slug.replace(ch, dst)
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_-]+', '-', slug)
    return slug.strip('-')


# ── core scrape function ──────────────────────────────────────────────────────

def scrape_city_day(conn, city_def, day_def, menu_date: date):
    """Scrape one city × one day. Returns item count or 0."""
    url      = day_url(city_def["url"], day_def["slug"])
    date_str = menu_date.isoformat()
    now      = datetime.now().isoformat()
    weekday  = day_def["weekday"]

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"    ✗ network error: {e}")
        return 0

    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    restaurants_on_page = [h2 for h2 in soup.find_all("h2") if h2.find("a")]
    if not restaurants_on_page:
        print(f"    ✗ no data (Coming Soon or empty page)")
        return 0

    city_id = get_or_create_city(
        conn, city_def["name"], city_def["slug"], city_def["url"],
        lat=city_def.get("lat"), lon=city_def.get("lon"),
    )
    run_id = upsert_scrape_run(conn, city_id, SOURCE, now, date_str)
    city_items = 0

    for h2 in restaurants_on_page:
        name_tag = h2.find("a")
        name     = name_tag.text.strip()
        url_rest = name_tag.get("href", "")
        slug     = make_slug(name)

        siblings = []
        node = h2.find_next_sibling()
        while node and node.name != "h2":
            siblings.append(node)
            node = node.find_next_sibling()

        full_text = " ".join(s.get_text(" ", strip=True) for s in siblings)
        phone_m   = re.search(r'[\+0][\d\s/]{7,}', full_text)
        phone     = phone_m.group(0).strip() if phone_m else ""
        delivery  = any("delivery_green" in img.get("src", "")
                        for s in siblings for img in s.find_all("img"))
        address   = ""
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

        rest_id = upsert_restaurant(conn, city_id, name, slug, url_rest,
                                    address, phone, delivery, info)
        update_restaurant_active_days(conn, rest_id, weekday, date_str)

        raw_lines = []
        for s in siblings:
            table = s if s.name == "table" else s.find("table")
            if not table: continue
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    lines = [l.strip() for l in cells[1].get_text("\n", strip=True).split("\n") if l.strip()]
                    raw_lines.extend(lines)

        groups = group_lines_into_items(raw_lines)
        items  = [parse_item(g) for g in groups]

        conn.executemany("""
            INSERT INTO menu_items
                (restaurant_id, scrape_run_id, type, name, description,
                 weight, price_eur, menu_price, allergens,
                 kcal, protein_g, fat_g, carbs_g, fiber_g,
                 tags, raw)
            VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?)
        """, [
            (
                rest_id, run_id,
                i["type"], i["name"], i["description"],
                i["weight"], i["price_eur"], menu_price,
                json.dumps(i["allergens"], ensure_ascii=False) if i["allergens"] else "[]",
                i["kcal"], i["protein_g"], i["fat_g"], i["carbs_g"], i["fiber_g"],
                json.dumps(i["tags"], ensure_ascii=False),
                i["raw"],
            )
            for i in items
        ])
        conn.commit()
        city_items += len(items)
        print(f"      {name}: {len(items)} items")

    if city_items == 0:
        conn.execute("DELETE FROM scrape_runs WHERE id=?", (run_id,))
        conn.commit()
        print(f"    ✗ 0 items — run discarded")
    else:
        update_city_restaurant_count(conn, city_id)

    return city_items


def scrape(days_to_scrape=None):
    conn = connect()
    init_db(conn)

    if days_to_scrape is None:
        days_to_scrape = DAYS

    grand_total = 0

    for city_def in CITIES:
        print(f"\n── {city_def['name']} ──")
        city_total = 0
        for day_def in days_to_scrape:
            menu_date = week_date_for(day_def["weekday"])
            print(f"  weekday {day_def['weekday']} ({menu_date})")
            count = scrape_city_day(conn, city_def, day_def, menu_date)
            city_total  += count
            grand_total += count
        if city_total > 0:
            print(f"  → {city_total} items total")

    conn.close()
    print(f"\n✓ Done: {grand_total} items total")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--today" in args:
        today_weekday = date.today().weekday()
        matching = [d for d in DAYS if d["weekday"] == today_weekday]
        if not matching:
            print("Today is a weekend — namenu only has Mon–Fri menus.")
            sys.exit(0)
        scrape(days_to_scrape=matching)

    elif "--day" in args:
        idx  = args.index("--day")
        slug = args[idx + 1] if idx + 1 < len(args) else ""
        matching = [d for d in DAYS if d["slug"] == slug]
        if not matching:
            print(f"Unknown day '{slug}'. Valid: {', '.join(d['slug'] for d in DAYS)}")
            sys.exit(1)
        scrape(days_to_scrape=matching)

    else:
        scrape()