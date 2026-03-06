# namenu+ &nbsp;[![beta](https://img.shields.io/badge/status-beta-yellow)](https://namenuplus.toomcis.eu) [![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

> Structured lunch menus from Slovak restaurants, served as a clean REST API.

namenu+ scrapes daily lunch menus from [namenu.sk](https://namenu.sk) and exposes them via a JSON API. Filter by city, day, allergens, price, and delivery availability. Built to be self-hostable and easy to extend with new cities or scraper sources.

**Currently covering:** Levice 🇸🇰 — more cities as namenu.sk expands.

---

## Project structure

```
namenuPlus/
├── api.py                      # FastAPI REST API + admin dashboard
├── main.py                     # CLI — key management & scrape log
├── scrapeAll.sh                # runs all scrapers in sequence
├── start.sh                    # container entrypoint
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .gitignore
├── scrapers/
│   ├── db.py                   # shared DB helpers for all scrapers
│   └── namenu.scrape.py        # namenu.sk scraper (multi-city, full week)
├── static/                     # static assets served by the API
└── webUI/
    ├── index.html              # admin dashboard UI
    ├── favicon.ico
    └── locales/
        ├── en.json
        ├── sk.json
        └── cs.json
```

---

## How it works

On startup the container immediately runs a full scrape so the DB is never empty on first boot. After that, a cron job re-scrapes at **06:00, 12:00, 18:00, and 00:00** every day. You can also trigger a scrape manually via the admin dashboard or CLI.

Two separate SQLite databases are used:

| File | Contains |
|---|---|
| `main.db` | API keys, scrape audit log |
| `namenu.db` | Cities, restaurants, menus, scrape runs |

This keeps auth data separate from scraped data — you can wipe `namenu.db` without touching your keys.

---

## API

**Base URL:** `https://api.namenuplus.toomcis.eu`

All requests require an `Authorization` header containing your API key.

```http
GET /api/levice/menu
Authorization: your-api-key-here
```

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/cities` | List all available cities |
| `GET` | `/api/{city}/week` | Which days have data this week |
| `GET` | `/api/{city}/restaurants` | Restaurants for a city on a given date |
| `GET` | `/api/{city}/restaurants/{slug}` | Full menu for one restaurant |
| `GET` | `/api/{city}/menu` | All dishes, filterable |

### `/api/{city}/menu` query params

| Param | Type | Description |
|---|---|---|
| `date` | `YYYY-MM-DD` | Menu date, defaults to today |
| `type` | `soup \| main \| dessert` | Dish type filter |
| `delivery` | `bool` | Delivery-only restaurants |
| `max_price` | `float` | Max price in EUR |
| `exclude_allergens` | `1,7,14` | EU allergen numbers to exclude (comma-separated) |
| `limit` | `int` | Max results (default 50, max 200) |
| `offset` | `int` | Pagination offset |

Interactive docs available at [`api.namenuplus.toomcis.eu/docs`](https://api.namenuplus.toomcis.eu/docs)

---

## Getting an API key

namenu+ uses private API keys to prevent abuse. Keys are **free** and issued manually.

The easiest way is to visit [namenuplus.toomcis.eu](https://namenuplus.toomcis.eu), scroll to the bottom, and use the email template button — just fill in the blanks and send. Alternatively email [contact@toomcis.eu](mailto:contact@toomcis.eu?subject=namenu%2B%20API%20Key%20Request) directly.

---

## Self-hosting with Docker

The recommended way to run namenu+ is via the published Docker image.

### Quick start

```bash
# pull and run
docker run -d \
  --name namenuPlus-API \
  -p 2332:2332 \
  -e PORT=2332 \
  -e MAIN_DB=/app/data/main.db \
  -e NAMENU_DB=/app/data/namenu.db \
  -v namenu_data:/app/data \
  ghcr.io/toomcis/namenuplus:latest
```

### With docker-compose (recommended)

```yaml
services:
  namenuPlus:
    image: ghcr.io/toomcis/namenuplus:latest
    restart: unless-stopped
    container_name: namenuPlus-API
    environment:
      - PORT=2332
      - MAIN_DB=/app/data/main.db
      - NAMENU_DB=/app/data/namenu.db
    ports:
      - "2332:2332"
    volumes:
      - namenu_data:/app/data

volumes:
  namenu_data:
```

```bash
docker compose up -d
```

On first boot, watch the logs for your admin API key — it is printed clearly right before the server starts:

```bash
docker logs namenuPlus-API
```

```
==============================
 namenu+ ready
==============================
API key created for 'admin':
  <your-key-here>
Save this — it won't be shown again.
==============================
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Port the API listens on |
| `MAIN_DB` | `main.db` | Path to the auth database |
| `NAMENU_DB` | `namenu.db` | Path to the menu data database |

### ⚠️ Keep your image up to date

namenu+ is under active development. When self-hosting, **always pull the latest image** before reporting bugs or issues. The project is working toward automatic version detection — once implemented, your instance will warn you if it is behind the main branch without stopping functionality.

```bash
docker compose pull && docker compose up -d
```

---

## Admin dashboard

The admin dashboard is available at `http://localhost:2332/` and is **intentionally not exposed publicly**. If you are proxying namenu+ behind a reverse proxy (nginx, Caddy, etc.), make sure to block or restrict access to `/` and `/admin/*` for any public-facing domain. Only allow those routes from your local network or trusted IPs.

Example nginx rule:

```nginx
server {
    server_name api.namenuplus.toomcis.eu;

    # restrict dashboard and admin routes to local access only
    location ~ ^/(admin.*|)$ {
        allow 127.0.0.1;
        allow 192.168.0.0/16;
        deny all;
    }

    # proxy everything else through
    location / {
        proxy_pass http://127.0.0.1:2332;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Local development

```bash
git clone https://github.com/toomcis/namenuPlus.git
cd namenuPlus

pip install -r requirements.txt

# create your first API key
python main.py --add-key "dev"

# scrape today's menus
python -X utf8 scrapers/namenu.scrape.py --today

# start the API
uvicorn api:app --reload
# → http://127.0.0.1:8000
```

### Scraper commands

```bash
./scrapeAll.sh                        # full week, all sources
./scrapeAll.sh --today                # just today
./scrapeAll.sh --day pondelok         # specific day

python -X utf8 scrapers/namenu.scrape.py --today
python -X utf8 scrapers/namenu.scrape.py --day streda
```

**Slovak day slugs:** `pondelok` `utorok` `streda` `stvrtok` `piatok`

### Key management (CLI)

```bash
python main.py --add-key "label"      # create a key
python main.py --list-keys            # list all keys
python main.py --revoke-key 3         # revoke key #3
python main.py --scrape-log           # show recent scrape audit log
```

---

## Adding a new scraper source

1. Create `scrapers/yoursite.scrape.py`
2. Import the shared DB helpers:
   ```python
   from scrapers.db import connect, init_db, get_or_create_city, upsert_restaurant, upsert_scrape_run
   ```
3. Set `SOURCE = "yoursite"` so runs are tracked separately
4. Add a line to `scrapeAll.sh`:
   ```bash
   python -X utf8 scrapers/yoursite.scrape.py $ARGS
   ```

The schema supports multiple sources per city per date via the `source` column on `scrape_runs` — re-scraping replaces data for that source, it does not stack.

---

## Database schema

**`main.db`** — auth & audit

```sql
api_keys    id, key_hash, label, created_at, last_used, active
scrape_log  id, source, started_at, finished_at, status, items, error
```

**`namenu.db`** — menu data

```sql
cities        id, name, slug, url
restaurants   id, city_id, name, slug, url, address, phone, delivery, info
scrape_runs   id, city_id, source, scraped_at, day, date
              UNIQUE(city_id, source, date)
menu_items    id, restaurant_id, scrape_run_id, type, name, description,
              weight, price_eur, menu_price, allergens, nutrition, raw
```

---

## Data & privacy

namenu+ currently **does not collect any user data**. It only stores scraped restaurant and menu information from public sources.

In a future update, the backend will introduce **opt-in** anonymous usage data collection (e.g. popular dishes, peak usage times) to enable better recommendations and improve the overall experience. This will always be opt-in, clearly documented, and when self-hosting, all data stays entirely on your own machine.

---

## Roadmap

- [x] Docker image + compose setup
- [x] Multi-city support
- [x] Admin dashboard with scrape history
- [x] Allergen and price filtering
- [x] EN / SK / CS localization
- [ ] More cities as namenu.sk expands
- [ ] Additional scraper sources beyond namenu.sk
- [ ] Automatic version detection with behind-branch warnings
- [ ] Webhook support — get notified when today's menu is scraped

---

## Contributing

PRs welcome, especially for:

- **Translations** — if you can improve or fix `SK / CZ / EN` strings, edit the files in [`webUI/locales/`](webUI/locales/) and [`webUI/index.html`](webUI/index.html)
- **New cities** — if you know of a city portal with structured lunch menus
- **New scraper sources** — any Slovak or Czech lunch aggregator
- **Parser improvements** — the namenu scraper handles many edge cases but there's always more

Open an issue before starting anything large so we can align first.

---

## License

MIT — do whatever, just don't pretend you made it.

---

*Made in Levice 🇸🇰 by [toomcis](https://toomcis.eu)*