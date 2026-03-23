# tester.py
# ToMenu API test runner — validates all public and admin API endpoints.
# Reads config from test-data.json (base_url, api_key, expected values).
#
# Usage:
#   pip install rich requests
#   python tester.py
#   python tester.py --config path/to/test-data.json

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich.theme import Theme

# ── theme ─────────────────────────────────────────────────────────────────────

THEME = Theme({
    "pass":    "bold green",
    "fail":    "bold red",
    "skip":    "dim yellow",
    "info":    "dim white",
    "section": "bold cyan",
    "detail":  "dim red",
    "url":     "dim blue",
    "header":  "bold white",
})

console = Console(theme=THEME, highlight=False)

# ── data structures ───────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name:    str
    passed:  bool
    message: str = ""
    url:     str = ""
    elapsed: float = 0.0


@dataclass
class Section:
    name:    str
    results: list[TestResult] = field(default_factory=list)

    @property
    def passed(self): return sum(1 for r in self.results if r.passed)
    @property
    def failed(self): return sum(1 for r in self.results if not r.passed)


# ── http helpers ──────────────────────────────────────────────────────────────

class ApiClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session  = requests.Session()
        self.session.headers["Authorization"] = api_key

    def get(self, path: str, **kwargs) -> tuple[Optional[Any], float, str]:
        url   = f"{self.base_url}{path}"
        start = time.perf_counter()
        try:
            resp    = self.session.get(url, timeout=10, **kwargs)
            elapsed = time.perf_counter() - start
            try:
                data = resp.json()
            except Exception:
                data = None
            return data, elapsed, url
        except requests.exceptions.ConnectionError:
            return None, time.perf_counter() - start, url
        except Exception:
            return None, time.perf_counter() - start, url

    def post(self, path: str, json_body: dict, auth: bool = True, **kwargs) -> tuple[Optional[Any], float, str, int]:
        url   = f"{self.base_url}{path}"
        start = time.perf_counter()
        try:
            headers = {"Content-Type": "application/json"}
            if not auth:
                headers.pop("Authorization", None)
                resp = requests.post(url, json=json_body, headers=headers, timeout=10, **kwargs)
            else:
                resp = self.session.post(url, json=json_body, timeout=10, **kwargs)
            elapsed = time.perf_counter() - start
            try:
                data = resp.json()
            except Exception:
                data = None
            return data, elapsed, url, resp.status_code
        except Exception:
            return None, time.perf_counter() - start, url, 0

    def patch(self, path: str, json_body: dict) -> tuple[Optional[Any], float, str, int]:
        url   = f"{self.base_url}{path}"
        start = time.perf_counter()
        try:
            resp    = self.session.patch(url, json=json_body, timeout=10)
            elapsed = time.perf_counter() - start
            try:
                data = resp.json()
            except Exception:
                data = None
            return data, elapsed, url, resp.status_code
        except Exception:
            return None, time.perf_counter() - start, url, 0

    def status_code(self, path: str, session=None) -> int:
        url = f"{self.base_url}{path}"
        try:
            s = session or self.session
            return s.get(url, timeout=5).status_code
        except Exception:
            return 0


# ── assertion helpers ─────────────────────────────────────────────────────────

def assert_keys(obj: dict, keys: list[str], context: str = "") -> tuple[bool, str]:
    missing = [k for k in keys if k not in obj]
    if missing:
        return False, f"Missing fields in {context or 'object'}: {', '.join(missing)}"
    return True, ""


def assert_list_not_empty(data: Any, name: str) -> tuple[bool, str]:
    if not isinstance(data, list) or len(data) == 0:
        return False, f"Expected non-empty list for '{name}', got: {type(data).__name__}"
    return True, ""


# ── test runner ───────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, client: ApiClient, cfg: dict):
        self.client   = client
        self.cfg      = cfg
        self.expected = cfg.get("expected", {})
        self.sections: list[Section] = []
        self._current: Optional[Section] = None

        self._cities:      list[dict] = []
        self._restaurants: list[dict] = []
        self._items:       list[dict] = []
        self._first_city:  str        = cfg.get("known_city_slug", "levice")
        self._first_rest:  Optional[str] = cfg.get("known_restaurant_slug")
        self._first_date:  Optional[str] = cfg.get("known_date")
        self._bug_id:      Optional[int] = None

    def section(self, name: str):
        self._current = Section(name)
        self.sections.append(self._current)

    def record(self, name: str, passed: bool, message: str = "", url: str = "", elapsed: float = 0.0):
        self._current.results.append(TestResult(name, passed, message, url, elapsed))

    def ok(self, name: str, url: str = "", elapsed: float = 0.0):
        self.record(name, True, "", url, elapsed)

    def fail(self, name: str, message: str, url: str = "", elapsed: float = 0.0):
        self.record(name, False, message, url, elapsed)

    def run_all(self):
        self._test_cities()
        self._test_restaurants()
        self._test_restaurant_detail()
        self._test_menu()
        self._test_week()
        self._test_feed()
        self._test_admin_stats()
        self._test_admin_tags()
        self._test_admin_system()
        self._test_admin_cron()
        self._test_bugs()
        self._test_ntfy()
        self._test_auth()
        self._test_404s()

    # ── cities ────────────────────────────────────────────────────────────────

    def _test_cities(self):
        self.section("GET /api/cities")
        data, elapsed, url = self.client.get("/api/cities")

        if data is None:
            self.fail("API reachable", "Could not connect to server — is it running?", url)
            return
        self.ok("API reachable", url, elapsed)

        ok, msg = assert_list_not_empty(data, "cities")
        if not ok:
            self.fail("Returns list of cities", msg, url)
            return
        self.ok("Returns list of cities", url)
        self._cities = data

        min_c = self.expected.get("min_cities", 1)
        self.record(f"At least {min_c} city returned", len(data) >= min_c,
                    f"Got {len(data)}", url)

        fields = self.expected.get("city_fields", [])
        ok, msg = assert_keys(data[0], fields, "cities[0]")
        self.record("Required fields present", ok, msg, url)

        for f in ("lat", "lon", "restaurant_count"):
            self.record(f"Optional field '{f}' present", f in data[0],
                        f"Field '{f}' missing" if f not in data[0] else "", url)

        known = self.cfg.get("known_city_slug")
        if known:
            slugs = [c["slug"] for c in data]
            ok = known in slugs
            self.record(f"Known city '{known}' in results", ok,
                        f"'{known}' not found. Available: {slugs}" if not ok else "", url)
            if ok:
                self._first_city = known

    # ── restaurants ───────────────────────────────────────────────────────────

    def _test_restaurants(self):
        city = self._first_city
        self.section(f"GET /api/cities/{{city}}/restaurants")
        data, elapsed, url = self.client.get(f"/api/cities/{city}/restaurants")

        if data is None:
            self.fail("Request succeeds", "Got None — server error or city not found", url)
            return
        self.ok("Request succeeds", url, elapsed)

        rests = data.get("restaurants", [])
        self.record("Response has 'restaurants' key", "restaurants" in data, "", url)
        self.record("Response has 'date' key", "date" in data, "", url)
        self.record("Response has 'count' key", "count" in data, "", url)

        ok, msg = assert_list_not_empty(rests, "restaurants")
        if not ok:
            self.fail("At least one restaurant returned", msg, url)
            return
        self.ok("At least one restaurant returned", url)
        self._restaurants = rests

        fields = self.expected.get("restaurant_fields", [])
        ok, msg = assert_keys(rests[0], fields, "restaurants[0]")
        self.record("Required fields present", ok, msg, url)
        self.record("'active_days' is a list", isinstance(rests[0].get("active_days"), list), "", url)

        data2, _, url2 = self.client.get(f"/api/cities/{city}/restaurants",
                                          params={"delivery": "true"})
        if data2 and "restaurants" in data2:
            non_del = [r for r in data2["restaurants"] if not r.get("delivery")]
            self.record("Delivery filter excludes non-delivery", len(non_del) == 0,
                        f"{len(non_del)} non-delivery slipped through", url2)
        else:
            self.fail("Delivery filter works", "No response", url2)

        if self._restaurants:
            self._first_rest = self._restaurants[0]["slug"]

    # ── restaurant detail ─────────────────────────────────────────────────────

    def _test_restaurant_detail(self):
        city = self._first_city
        slug = self._first_rest
        if not slug:
            self.section("GET /api/cities/{city}/restaurants/{slug}")
            self.fail("Test precondition", "No restaurant slug available — skipping")
            return

        self.section(f"GET /api/cities/{{city}}/restaurants/{{slug}}")
        data, elapsed, url = self.client.get(f"/api/cities/{city}/restaurants/{slug}")

        if data is None or "detail" in (data or {}):
            self.fail("Request succeeds", f"Error: {data}", url)
            return
        self.ok("Request succeeds", url, elapsed)

        self.record("Has 'menu' key",   "menu" in data, "", url)
        self.record("Has 'name' field", "name" in data, "", url)
        self.record("Has 'slug' field", bool(data.get("slug")), "", url)
        self.record("Has 'date' field", "date" in data, "", url)

        menu = data.get("menu", [])
        if menu:
            self.ok("Menu is non-empty", url)
            fields = self.expected.get("menu_item_fields", [])
            ok, msg = assert_keys(menu[0], fields, "menu[0]")
            self.record("Menu item has required fields", ok, msg, url)

            valid_types = self.expected.get("item_types", ["soup", "main", "dessert"])
            bad = [i["type"] for i in menu if i.get("type") not in valid_types]
            self.record("All item types valid", len(bad) == 0, f"Bad types: {bad[:5]}", url)
            self.record("'tags' field is list", isinstance(menu[0].get("tags"), list), "", url)
        else:
            self.record("Menu is non-empty", False,
                        "Restaurant exists but has no menu items for this date", url)

    # ── menu ──────────────────────────────────────────────────────────────────

    def _test_menu(self):
        city = self._first_city
        self.section("GET /api/cities/{city}/menu")
        data, elapsed, url = self.client.get(f"/api/cities/{city}/menu")

        if data is None:
            self.fail("Request succeeds", "Got None", url)
            return
        self.ok("Request succeeds", url, elapsed)

        self.record("Has 'items' key", "items" in data, "", url)
        self.record("Has 'count' key", "count" in data, "", url)
        self.record("Has 'date' key",  "date" in data, "", url)

        items = data.get("items", [])
        if items:
            self._items = items
            self.ok("Items list non-empty", url)
            fields = self.expected.get("menu_item_fields", [])
            ok, msg = assert_keys(items[0], fields, "items[0]")
            self.record("Required fields present", ok, msg, url)
            self.record("'tags' field is list", isinstance(items[0].get("tags"), list), "", url)
        else:
            self.fail("Items list non-empty",
                      "No items — is there scraped data for today?", url)

        for t in ("soup", "main", "dessert"):
            d, _, u = self.client.get(f"/api/cities/{city}/menu", params={"type": t})
            if d and "items" in d:
                wrong = [i for i in d["items"] if i.get("type") != t]
                self.record(f"Type filter '{t}' works", len(wrong) == 0,
                            f"{len(wrong)} items have wrong type", u)
            else:
                self.fail(f"Type filter '{t}' works", "No response", u)

        d, _, u = self.client.get(f"/api/cities/{city}/menu",
                                   params={"exclude_allergens": "7"})
        self.record("Allergen exclusion returns response", bool(d and "items" in d),
                    "No response", u)

        d1, _, u1 = self.client.get(f"/api/cities/{city}/menu",
                                     params={"limit": 5, "offset": 0})
        d2, _, u2 = self.client.get(f"/api/cities/{city}/menu",
                                     params={"limit": 5, "offset": 5})
        if d1 and d2:
            ids1 = {i.get("id") for i in d1.get("items", [])}
            ids2 = {i.get("id") for i in d2.get("items", [])}
            overlap = ids1 & ids2
            self.record("Pagination: no overlap between page 0 and page 1",
                        len(overlap) == 0, f"Overlapping ids: {overlap}", u2)
        else:
            self.fail("Pagination works", "Could not fetch both pages", u1)

    # ── week ──────────────────────────────────────────────────────────────────

    def _test_week(self):
        city = self._first_city
        self.section("GET /api/cities/{city}/week")
        data, elapsed, url = self.client.get(f"/api/cities/{city}/week")

        if not isinstance(data, list):
            self.fail("Returns list", f"Got: {type(data).__name__}", url)
            return
        self.ok("Returns list", url, elapsed)
        self.record("Returns exactly 5 entries (Mon–Fri)", len(data) == 5,
                    f"Got {len(data)}", url)

        if data:
            fields = self.expected.get("week_entry_fields", [])
            ok, msg = assert_keys(data[0], fields, "week[0]")
            self.record("Required fields present", ok, msg, url)

            dates = [e["date"] for e in data]
            self.record("Dates are in order", dates == sorted(dates),
                        f"Not sorted: {dates}", url)

            bad_bool = [e for e in data if not isinstance(e.get("has_data"), bool)]
            self.record("has_data is boolean for all entries", len(bad_bool) == 0,
                        f"{len(bad_bool)} entries have non-bool has_data", url)

    # ── feed ──────────────────────────────────────────────────────────────────

    def _test_feed(self):
        city = self._first_city
        self.section("GET /api/feed")
        data, elapsed, url = self.client.get("/api/feed",
                                              params={"city": city, "user_id": 1})

        if data is None:
            self.fail("Request succeeds", "Got None", url)
            return
        self.ok("Request succeeds", url, elapsed)

        self.record("Has 'items' key",   "items" in data, "", url)
        self.record("Has 'count' key",   "count" in data, "", url)
        self.record("Has 'user_id' key", "user_id" in data, "", url)
        self.record("Has 'date' key",    "date" in data, "", url)

        items = data.get("items", [])
        if items:
            self.ok("Feed is non-empty", url)
            fields = self.expected.get("feed_item_fields", [])
            ok, msg = assert_keys(items[0], fields, "items[0]")
            self.record("Required fields present", ok, msg, url)
            non_main = [i for i in items if i.get("type") != "main"]
            self.record("Feed only returns main dishes", len(non_main) == 0,
                        f"{len(non_main)} non-main items", url)
        else:
            self.record("Feed is non-empty", False,
                        "Feed returned 0 items", url)

        d2, _, u2 = self.client.get("/api/feed",
                                     params={"city": city, "user_id": 1, "limit": 3})
        if d2 and "items" in d2:
            self.record("Limit param respected", len(d2["items"]) <= 3,
                        f"Got {len(d2['items'])} with limit=3", u2)
        else:
            self.fail("Limit param works", "No response", u2)

    # ── admin stats ───────────────────────────────────────────────────────────

    def _test_admin_stats(self):
        self.section("GET /admin/stats")
        data, elapsed, url = self.client.get("/admin/stats")

        if data is None:
            self.fail("Request succeeds", "Got None or connection error", url)
            return
        self.ok("Request succeeds", url, elapsed)

        for key in ("restaurants", "items_today", "scrape_runs", "recent_runs", "city_breakdown"):
            self.record(f"Has '{key}' field", key in data,
                        f"Missing '{key}' — keys: {list(data.keys())}", url)

    # ── admin tags ────────────────────────────────────────────────────────────

    def _test_admin_tags(self):
        self.section("GET /admin/tags")
        data, elapsed, url = self.client.get("/admin/tags")

        if data is None:
            self.fail("Request succeeds", "Got None", url)
            return
        self.ok("Request succeeds", url, elapsed)

        self.record("Has 'tags' field", "tags" in data, "", url)
        self.record("Has 'total_items' field", "total_items" in data, "", url)

        tags = data.get("tags", [])
        if tags:
            self.ok("Tags list non-empty", url)
            ok, msg = assert_keys(tags[0], ["tag", "count", "pct"], "tags[0]")
            self.record("Tag entry has required fields", ok, msg, url)

            # spot-check a known tag
            tag_names = [t["tag"] for t in tags]
            self.record("Common tag 'meat' present", "meat" in tag_names,
                        f"Tags found: {tag_names[:10]}", url)

            # test detail endpoint for first tag
            first_tag = tags[0]["tag"]
            d2, _, u2 = self.client.get(f"/admin/tags/{first_tag}")
            if d2 and "items" in d2:
                self.ok(f"GET /admin/tags/{{tag}} returns items", u2)
            else:
                self.fail(f"GET /admin/tags/{{tag}} returns items",
                          f"Got: {d2}", u2)
        else:
            self.record("Tags list non-empty", False,
                        "No tags found — scrape data may have no tagged items", url)

    # ── admin system ──────────────────────────────────────────────────────────

    def _test_admin_system(self):
        self.section("GET /admin/system")
        data, elapsed, url = self.client.get("/admin/system")

        if data is None:
            self.fail("Request succeeds", "Got None", url)
            return
        self.ok("Request succeeds", url, elapsed)

        for key in ("cities", "restaurants", "scrape_runs", "menu_items",
                    "namenu_db_bytes", "main_db_bytes", "cron_schedule"):
            self.record(f"Has '{key}' field", key in data,
                        f"Missing '{key}'", url)

    # ── admin cron ────────────────────────────────────────────────────────────

    def _test_admin_cron(self):
        self.section("PATCH /admin/cron")

        # read current schedule first
        data, _, url = self.client.get("/admin/system")
        original = (data or {}).get("cron_schedule", "0 6,12,18,0 * * 1-5")

        # patch with a valid schedule
        test_schedule = "0 7,13 * * 1-5"
        d, elapsed, u, code = self.client.patch("/admin/cron",
                                                 {"schedule": test_schedule})
        self.record("Valid schedule accepted", code == 200,
                    f"Got status {code}: {d}", u, elapsed)

        # invalid schedule (4 fields) should 422
        d2, _, u2, code2 = self.client.patch("/admin/cron",
                                              {"schedule": "bad cron"})
        self.record("Invalid schedule rejected (422)", code2 == 422,
                    f"Got status {code2}", u2)

        # restore original
        self.client.patch("/admin/cron", {"schedule": original})

    # ── bug reporting ─────────────────────────────────────────────────────────

    def _test_bugs(self):
        self.section("POST /bugs  (public)")

        # valid report — no auth needed
        body = {
            "city_slug":       self._first_city,
            "restaurant_slug": self._first_rest or "test-restaurant",
            "type":            "wrong_tag",
            "description":     "tester.py automated bug report — safe to ignore",
        }
        data, elapsed, url, code = self.client.post("/bugs", body, auth=False)
        self.record("Valid bug report accepted (200)", code == 200,
                    f"Got {code}: {data}", url, elapsed)

        # invalid type should 422
        bad_body = {**body, "type": "not_a_real_type"}
        _, _, u2, code2 = self.client.post("/bugs", bad_body, auth=False)
        self.record("Invalid bug type rejected (422)", code2 == 422,
                    f"Got status {code2}", u2)

        # list bugs — needs auth
        self.section("GET /admin/bugs")
        bdata, elapsed2, burl = self.client.get("/admin/bugs")
        if bdata is None:
            self.fail("Request succeeds", "Got None", burl)
            return
        self.ok("Request succeeds", burl, elapsed2)

        self.record("Returns a list", isinstance(bdata, list),
                    f"Got: {type(bdata).__name__}", burl)

        if isinstance(bdata, list) and bdata:
            self._bug_id = bdata[0]["id"]
            ok, msg = assert_keys(bdata[0],
                ["id", "city_slug", "restaurant_slug", "type", "status", "created_at"],
                "bugs[0]")
            self.record("Bug entry has required fields", ok, msg, burl)

            # patch it to resolved
            if self._bug_id:
                d3, _, u3, code3 = self.client.patch(
                    f"/admin/bugs/{self._bug_id}", {"status": "resolved"}
                )
                self.record("PATCH /admin/bugs/{id} resolves bug", code3 == 200,
                            f"Got {code3}: {d3}", u3)

    # ── ntfy config ───────────────────────────────────────────────────────────

    def _test_ntfy(self):
        self.section("GET+PATCH /admin/ntfy")

        # read
        data, elapsed, url = self.client.get("/admin/ntfy")
        if data is None:
            self.fail("GET /admin/ntfy succeeds", "Got None", url)
            return
        self.ok("GET /admin/ntfy succeeds", url, elapsed)

        for key in ("server_url", "topic", "private"):
            self.record(f"Has '{key}' field", key in data,
                        f"Missing '{key}'", url)

        self.record("auth_token not exposed in GET response",
                    "auth_token" not in data,
                    "auth_token should be stripped from GET response", url)

        # patch
        d2, _, u2, code = self.client.patch("/admin/ntfy", {
            "server_url": "https://ntfy.sh",
            "topic":      "tomenu-test",
            "private":    False,
        })
        self.record("PATCH /admin/ntfy accepted", code == 200,
                    f"Got {code}: {d2}", u2)

    # ── auth ──────────────────────────────────────────────────────────────────

    def _test_auth(self):
        self.section("Authentication")

        bad = requests.Session()
        bad.headers["Authorization"] = "totallynotavalidkey"

        code = self.client.status_code("/api/cities", session=bad)
        self.record("Bad key returns 403", code == 403,
                    f"Got status {code} instead of 403")

        code2 = self.client.status_code("/api/cities", session=requests.Session())
        self.record("No key returns 401", code2 == 401,
                    f"Got status {code2} instead of 401")

        # public endpoint /bugs should NOT require auth
        code3 = self.client.status_code("/bugs", session=requests.Session())
        self.record("/bugs is public (no 401/403)", code3 not in (401, 403),
                    f"Got {code3} — /bugs should be a public endpoint")

    # ── 404s ──────────────────────────────────────────────────────────────────

    def _test_404s(self):
        self.section("404 handling")
        cases = [
            ("/api/cities/thiscitydoesnotexist/restaurants", "Non-existent city 404"),
            ("/api/cities/thiscitydoesnotexist/menu",        "Non-existent city menu 404"),
            (f"/api/cities/{self._first_city}/restaurants/this-restaurant-does-not-exist",
             "Non-existent restaurant 404"),
            ("/admin/tags/thisisnotarealtag",                "Non-existent tag 404"),
        ]
        for path, name in cases:
            code = self.client.status_code(path)
            self.record(name, code == 404, f"Expected 404, got {code}")


# ── rendering ─────────────────────────────────────────────────────────────────

def print_summary(sections: list[Section], total_elapsed: float) -> int:
    console.print()
    console.rule("[header]ToMenu API Test Results[/header]")
    console.print()

    total_pass = total_fail = 0

    for section in sections:
        console.print(f"  [section]{section.name}[/section]")
        for r in section.results:
            dots  = "." * max(2, 52 - len(r.name))
            if r.passed:
                time_s = f"[info]{r.elapsed*1000:.0f}ms[/info]" if r.elapsed > 0 else ""
                console.print(f"    [white]{r.name}[/white][info]{dots}[/info][pass]✓[/pass] {time_s}")
                total_pass += 1
            else:
                console.print(f"    [white]{r.name}[/white][info]{dots}[/info][fail]✗[/fail]")
                total_fail += 1
        console.print()

    console.rule()
    console.print(
        Text(f"  {total_pass} passed", style="pass"),
        Text(f"  {total_fail} failed", style="fail" if total_fail else "info"),
        Text(f"  {total_elapsed:.2f}s total", style="info"),
    )

    failures = [(s.name, r) for s in sections for r in s.results if not r.passed]
    if failures:
        console.print()
        console.rule("[fail]Failures[/fail]")
        for section_name, r in failures:
            console.print(f"\n  [fail]{section_name}[/fail] / [white]{r.name}[/white]")
            if r.message:
                console.print(f"    [detail]{r.message}[/detail]")
            if r.url:
                console.print(f"    [url]{r.url}[/url]")
        console.print()

    return total_fail

def json_summary(sections, total_elapsed):
    """Return structured JSON output for --json mode."""
    total_pass = sum(r.passed for s in sections for r in s.results)
    total_fail = sum(not r.passed for s in sections for r in s.results)
    return {
        "total_pass":    total_pass,
        "total_fail":    total_fail,
        "total_elapsed": round(total_elapsed, 3),
        "sections": [
            {
                "name":    s.name,
                "passed":  s.passed,
                "failed":  s.failed,
                "results": [
                    {
                        "name":    r.name,
                        "passed":  r.passed,
                        "message": r.message,
                        "url":     r.url,
                        "elapsed": round(r.elapsed * 1000, 1),
                    }
                    for r in s.results
                ],
            }
            for s in sections
        ],
    }

# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ToMenu API tester")
    parser.add_argument("--config", default="test-data.json")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of TUI")
    args = parser.parse_args()
    
    try:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        if args.json:
            print(json.dumps({"error": f"Config file not found: {args.config}"}))
        else:
            console.print(f"[fail]Config file not found:[/fail] {args.config}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        if args.json:
            print(json.dumps({"error": f"Invalid JSON in config: {e}"}))
        else:
            console.print(f"[fail]Invalid JSON in config:[/fail] {e}")
        sys.exit(1)
 
    base_url = cfg.get("base_url", "http://localhost:2332")
    api_key  = cfg.get("api_key", "")
 
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        if args.json:
            print(json.dumps({"error": "No API key set in test-data.json"}))
        else:
            console.print("[fail]No API key set.[/fail] Edit test-data.json and set 'api_key'.")
        sys.exit(1)
 
    if not args.json:
        console.print()
        console.print(Panel(
            f"[header]ToMenu API Tester[/header]\n"
            f"[info]Target:[/info]  [url]{base_url}[/url]\n"
            f"[info]City:[/info]    [white]{cfg.get('known_city_slug', 'levice')}[/white]",
            box=box.ROUNDED,
            expand=False,
        ))
 
    client  = ApiClient(base_url, api_key)
    runner  = Runner(client, cfg)
 
    start = time.perf_counter()
    if args.json:
        runner.run_all()
    else:
        with console.status("[info]Running tests...[/info]", spinner="dots"):
            runner.run_all()
    elapsed = time.perf_counter() - start
 
    if args.json:
        print(json.dumps(json_summary(runner.sections, elapsed), ensure_ascii=False))
        sys.exit(1 if any(not r.passed for s in runner.sections for r in s.results) else 0)
    else:
        n_fail = print_summary(runner.sections, elapsed)
        sys.exit(1 if n_fail > 0 else 0)
 
 
if __name__ == "__main__":
    main()