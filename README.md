# ToMenu

> Structured lunch menus from Slovak restaurants, served as a clean REST API.

ToMenu scrapes daily lunch menus from Slovak restaurants and exposes them as a filterable JSON API. Tag dishes by type, allergens, price, and delivery. Includes an admin dashboard, ML-assisted dish tagging, a personalised feed API, and push notifications via ntfy.

**Currently covering:** Levice 🇸🇰 — more cities coming.

---

## What's inside

```
.
├── api.py                  # FastAPI — public API + admin endpoints
├── main.py                 # CLI — key management, scrape log
├── restaurant_api.py       # Local restaurant data service (stand-in for restaurant.tomenu.sk)
├── restaurant_client.py    # HTTP client connecting api.py to the restaurant service
├── tester.py               # API test suite (--json for structured output)
├── test-data.json          # Test fixtures for tester.py
├── scrapeAll.sh            # Runs all scrapers in sequence
├── start.sh                # Docker entrypoint — starts both services + cron
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── scrapers/
│   ├── db.py               # Shared SQLite helpers for all scrapers
│   └── namenu.scrape.py    # namenu.sk scraper — multi-city, full week
├── ml/
│   ├── tagger.py           # ML tag model (TF-IDF + logistic regression)
│   └── seed_training_data.sql  # Community-verified training examples
└── webUI/
    └── index.html          # Admin dashboard (Alpine.js SPA)
```

---

## How it works

Two SQLite databases keep concerns separated:

| File          | Contains                                                      |
| ------------- | ------------------------------------------------------------- |
| `main.db`   | API keys, scrape audit log, ntfy config, bug reports          |
| `namenu.db` | Cities, restaurants, menus, scrape runs, ML training examples |

On first boot the container scrapes immediately so the DB is never empty. A cron job then re-scrapes at **06:00, 12:00, 18:00, and 00:00** on weekdays. You can also trigger scrapes manually from the dashboard or CLI.

Dishes are tagged automatically on every scrape using a  **hybrid system** :

* Rule-based keyword matching (always active, no training needed)
* ML model (TF-IDF + logistic regression) when trained — merged with rules

---

## API

All requests require an `Authorization` header with your API key.

```http
GET /api/cities
Authorization: your-api-key
```

### Endpoints

| Method  | Path                                      | Description                        |
| ------- | ----------------------------------------- | ---------------------------------- |
| `GET` | `/api/cities`                           | All cities with scrape data        |
| `GET` | `/api/cities/{city}/week`               | Which weekdays have data this week |
| `GET` | `/api/cities/{city}/restaurants`        | Restaurants for a city             |
| `GET` | `/api/cities/{city}/restaurants/{slug}` | One restaurant with full menu      |
| `GET` | `/api/cities/{city}/menu`               | All dishes, filterable             |
| `GET` | `/api/feed`                             | Personalised ranked dish feed      |

### `/api/cities/{city}/menu` filters

| Param                 | Type                  | Description                                  |
| --------------------- | --------------------- | -------------------------------------------- |
| `date`              | `YYYY-MM-DD`        | Defaults to today, falls back to most recent |
| `type`              | `soup\|main\|dessert` | Dish type                                    |
| `delivery`          | `bool`              | Delivery restaurants only                    |
| `max_price`         | `float`             | Max price in EUR                             |
| `exclude_allergens` | `1,7,14`            | EU allergen numbers to exclude               |
| `tags`              | `meat,vegetarian`   | Tag filter                                   |
| `limit`             | `int`               | Max results (default 50, max 200)            |
| `offset`            | `int`               | Pagination                                   |

### `/api/feed` params

| Param       | Type                       | Description                        |
| ----------- | -------------------------- | ---------------------------------- |
| `city`    | `string`                 | City slug                          |
| `user_id` | `int`                    | User identifier                    |
| `weights` | `meat:80,vegetarian:-60` | Tag preference weights for ranking |
| `limit`   | `int`                    | Max results (default 20, max 50)   |

---

## Self-hosting with Docker

### docker-compose (recommended)

```yaml
services:
  tomenu:
    image: ghcr.io/toomcis/tomenu:latest
    restart: unless-stopped
    container_name: tomenu-api
    environment:
      - PORT=2332
      - MAIN_DB=/app/data/main.db
      - NAMENU_DB=/app/data/namenu.db
    ports:
      - "2332:2332"
    volumes:
      - tomenu_data:/app/data

volumes:
  tomenu_data:
```

```bash
docker compose up -d
docker logs tomenu-api   # grab your admin key from the first boot output
```

First boot output:

```
==============================
 ToMenu ready
==============================
API key created for 'admin':
  <your-key-here>
Save this — it won't be shown again.
==============================
```

### docker run

```bash
docker run -d \
  --name tomenu-api \
  -p 2332:2332 \
  -e PORT=2332 \
  -e MAIN_DB=/app/data/main.db \
  -e NAMENU_DB=/app/data/namenu.db \
  -v tomenu_data:/app/data \
  ghcr.io/toomcis/tomenu:latest
```

### Environment variables

| Variable                | Default                   | Description                                 |
| ----------------------- | ------------------------- | ------------------------------------------- |
| `PORT`                | `8000`                  | Main API port                               |
| `RESTAURANT_API_PORT` | `6333`                  | Internal restaurant service port            |
| `MAIN_DB`             | `main.db`               | Auth database path                          |
| `NAMENU_DB`           | `namenu.db`             | Menu data database path                     |
| `RESTAURANT_API_URL`  | `http://localhost:6333` | Override to use a hosted restaurant service |

```bash
# Always pull before reporting bugs
docker compose pull && docker compose up -d
```

---

## Admin dashboard

Available at `http://localhost:2332/` after startup. Not meant to be public — restrict it at the proxy level:

```nginx
server {
    server_name api.tomenu.sk;

    location ~ ^/(admin|$) {
        allow 127.0.0.1;
        allow 192.168.0.0/16;
        deny all;
    }

    location / {
        proxy_pass http://127.0.0.1:2332;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Dashboard features

* **Overview** — stats, city heatmap with day selector, recent scrape runs
* **Data Health** — tag coverage, API health check, restaurants missing data
* **Menus** — browse scraped menus by city and day
* **Restaurants** — full restaurant list with filters
* **ML / Tags** — tester, live tagger, tag stats & inspector, bulk editor, model training
* **Feed Preview** — simulate user taste profiles and preview ranked feed
* **Scraper** — manual scrape with day selection, scrape history log
* **Notifications** — ntfy push config
* **System** — DB sizes, cron schedule, API keys

---

## ML tagging

Dishes are tagged on every scrape. The tagger is hybrid — rules always run, the ML model adds on top when trained.

**Available tags:** `meat` `chicken` `pork` `beef` `fish` `vegetarian` `vegan` `fried` `grilled` `baked` `steamed` `pasta` `rice` `salad` `soup` `burger` `sandwich` `pizza` `asian` `dessert` `dairy` `egg` `spicy` `sweet` `healthy`

### Training the model

1. Open the admin dashboard → **ML / Tags** → **Bulk Editor**
2. Search for dishes, select them, apply correct tags, hit **save + train**
3. Go to the **Train** tab and click **Train model**
4. The next scrape uses the new model automatically

The model trains only on human-verified examples. More diverse examples = better predictions. Aim for 10+ verified examples per tag before relying on the model.

### Sharing training data

Import the community seed examples to skip starting from zero:

```bash
sqlite3 /app/data/namenu.db < ml/seed_training_data.sql
```

Then retrain from the dashboard, or via CLI:

```bash
python -c "from ml.tagger import train; print(train('namenu.db'))"
```

Export your own verified examples to contribute back:

```bash
sqlite3 namenu.db \
  "SELECT 'INSERT OR IGNORE INTO ml_training_examples(item_id,item_name,tags,created_at) VALUES('||item_id||','''||replace(item_name,'''','''''')||''','''||tags||''','''||created_at||''');' FROM ml_training_examples;" \
  > ml/seed_training_data.sql
```

The trained model (`ml/model.pkl`) is excluded from git — regenerate it from the SQL seed data.

---

## Local development

```bash
git clone https://github.com/toomcis/tomenu.git
cd tomenu

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
./scrapeAll.sh                      # full week, all sources
./scrapeAll.sh --today              # today only
./scrapeAll.sh --day pondelok       # specific day

python -X utf8 scrapers/namenu.scrape.py --today
python -X utf8 scrapers/namenu.scrape.py --day streda
```

**Day slugs:** `pondelok` `utorok` `streda` `stvrtok` `piatok`

### Key management

```bash
python main.py --add-key "label"    # create
python main.py --list-keys          # list all
python main.py --revoke-key 3       # revoke by id
python main.py --scrape-log         # recent audit log
```

### Test suite

```bash
python tester.py            # human-readable
python tester.py --json     # JSON output (used by the dashboard health check)
```

---

## Push notifications (ntfy)

Configure under **Notifications** in the dashboard. Fires on:

* Scrape error or timeout
* Scrape completed with 0 items
* New bug report submitted from the app

Works with [ntfy.sh](https://ntfy.sh) (free) or a self-hosted ntfy instance.

---

## Adding a new scraper source

1. Create `scrapers/yoursite.scrape.py`
2. Use the shared DB helpers:
   ```python
   from scrapers.db import connect, init_db, get_or_create_city, \    upsert_restaurant, upsert_scrape_run
   ```
3. Set `SOURCE = "yoursite"` so runs are tracked separately
4. Register it in `scrapeAll.sh`:
   ```bash
   python -X utf8 scrapers/yoursite.scrape.py $ARGS
   ```

---

## .gitignore

```gitignore
# databases
*.db
*.db-shm
*.db-wal

# ML model — regenerated from seed_training_data.sql, don't commit the binary
ml/model.pkl
ml/meta.json

# Python
__pycache__/
*.pyc
.venv/
```

---

## Roadmap

* [X] Docker + compose
* [X] Multi-city scraping
* [X] Admin dashboard
* [X] Allergen and price filtering
* [X] Dish tagging — rules + ML hybrid
* [X] Human-in-the-loop ML training via bulk editor
* [X] Personalised feed with taste profile weights
* [X] Push notifications via ntfy
* [X] City heatmap with day selector
* [X] API health check endpoint
* [ ] More cities
* [ ] Additional scraper sources
* [ ] User accounts + swipe discovery UI
* [ ] Webhook support

---

## Contributing

PRs welcome — especially:

* **Training data** — export your verified examples and PR against `ml/seed_training_data.sql`
* **New cities** — any Slovak/Czech site with structured lunch menus
* **Parser improvements** — edge cases in the namenu scraper

Open an issue before starting anything big.

---

## License

MIT — do whatever, just don't pretend you made it.

---

*Made in Levice 🇸🇰 by [toomcis](https://toomcis.eu)*
