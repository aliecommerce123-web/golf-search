"""
Golf Search - Echtzeit-Preisabfrage für Belek Golf-Hotels.
Holt Live-Preise von bilyanagolf.com (Server-Side-Proxy + HTML-Parser).
Bei keinem Preis → Anfrage-Formular.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import asyncio

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.async_api import async_playwright, Browser, Playwright

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "golf_search.db"
BILYANA_DOMAIN = "https://bilyanagolf.com/"

# Reseller-Marge: der angezeigte Kundenpreis ist IMMER 5% günstiger als der
# günstigste Quellpreis (Bilyana oder Golfbelek). Ali-Anweisung 2026-05-29.
RESELLER_DISCOUNT = 0.05

# Quellen-Namen dürfen NIRGENDS kundenseitig erscheinen (Ali-Anweisung 2026-05-29).
# Nach außen (API-JSON + Frontend) wird nur ein neutraler Code geschickt; im
# Inquiry-Endpoint wird er serverseitig zurück auf den echten Namen gemappt,
# damit Alis DB-Audit weiß, über welchen Operator real gebucht wird.
_SOURCE_CODE = {"bilyana": "A", "golfbelek": "B"}
_SOURCE_FROM_CODE = {v: k for k, v in _SOURCE_CODE.items()}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BELEK_HOTELS = [
    {"oid": 1,  "name": "Cornelia Diamond Golf Resort and Spa", "stars": 5, "bilyana_oid": 65,  "golfbelek_id": 4},
    {"oid": 2,  "name": "Cullinan Belek",                     "stars": 5, "bilyana_oid": 347, "golfbelek_id": 13},
    {"oid": 3,  "name": "Gloria Golf Resort",                 "stars": 5, "bilyana_oid": 67,  "golfbelek_id": None},
    {"oid": 4,  "name": "Gloria Serenity Resort",             "stars": 5, "bilyana_oid": 69,  "golfbelek_id": None},
    {"oid": 5,  "name": "Gloria Verde Resort",                "stars": 5, "bilyana_oid": 68,  "golfbelek_id": None},
    {"oid": 6,  "name": "Kaya Belek",                         "stars": 5, "bilyana_oid": 80,  "golfbelek_id": 6},
    {"oid": 7,  "name": "Kaya Palazzo Golf Resort",           "stars": 5, "bilyana_oid": 74,  "golfbelek_id": 5},
    {"oid": 8,  "name": "Kempinski The Dome",                 "stars": 5, "bilyana_oid": None, "golfbelek_id": 8},
    {"oid": 9,  "name": "Lykia World Antalya",                "stars": 5, "bilyana_oid": None, "golfbelek_id": 21},
    {"oid": 10, "name": "Maxx Royal Belek Golf Resort",       "stars": 5, "bilyana_oid": 75,  "golfbelek_id": 16},
    {"oid": 11, "name": "Regnum Carya Golf and Spa Resort",     "stars": 5, "bilyana_oid": 72,  "golfbelek_id": 10},
    {"oid": 12, "name": "Regnum The Crown",                   "stars": 5, "bilyana_oid": None, "golfbelek_id": 20},
    {"oid": 13, "name": "Robinson Nobilis Belek",             "stars": 5, "bilyana_oid": 91,  "golfbelek_id": None},
    {"oid": 14, "name": "Sirene Belek",                       "stars": 5, "bilyana_oid": 66,  "golfbelek_id": 7},
    {"oid": 15, "name": "Sueno Hotels Deluxe Belek",          "stars": 5, "bilyana_oid": 71,  "golfbelek_id": 12},
    {"oid": 16, "name": "Sueno Hotels Golf Belek",            "stars": 5, "bilyana_oid": 70,  "golfbelek_id": 11},
    {"oid": 17, "name": "Titanic Deluxe Golf Belek",          "stars": 5, "bilyana_oid": 73,  "golfbelek_id": 14},
    {"oid": 18, "name": "Voyage Belek Golf and Spa",            "stars": 5, "bilyana_oid": 76,  "golfbelek_id": 17},
]
HOTEL_BY_OID = {h["oid"]: h for h in BELEK_HOTELS}

EXTRA_GOLF_COURSES = [
    "Antalya Golf Club: The Pasha Golf Course (18 Holes)",
    "Antalya Golf Club: PGA Sultan Golf Course (18 Holes)",
    "Carya Golf Club Belek (18 Holes)",
    "Cornelia Golf Club Faldo Course (27 Holes)",
    "Cullinan Links Aspendos Golf Course (18 Holes)",
    "Cullinan Links Olympos Golf Course (18 Holes)",
    "Gloria Golf Club New Golf Course (18 Holes)",
    "Gloria Golf Club Verde Golf Course (2 x 9 Holes)",
    "Gloria Golf Club Old Golf Course (18 Holes)",
    "Kaya Palazzo Golf Course (18 Holes)",
    "Lykia Links Golf Course (18 Holes)",
    "Montgomerie Maxx Royal Golf Course (18 Holes)",
    "National Golf Course (18 Holes)",
    "Robinson Nobilis Golf Course (18 Holes)",
    "Sueno Golf Club: Pines Golf Course Belek (18 Holes)",
    "Sueno Golf Club: Dunes Golf Course Belek (18 Holes)",
]

HOTEL_IMAGES_PATH = DATA_DIR / "hotel_images.json"


def _load_hotel_images() -> dict:
    """Laedt das lokale Bild-Manifest (oid -> Liste lokaler /static-Pfade)."""
    try:
        with open(HOTEL_IMAGES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): list(v) for k, v in raw.items() if v}
    except Exception:
        return {}


HOTEL_IMAGES = _load_hotel_images()


def _hotel_with_images(h: dict) -> dict:
    imgs = HOTEL_IMAGES.get(h["oid"], [])
    return {**h, "images": imgs, "cover": (imgs[0] if imgs else None)}


# Interne Quellen-Routing-IDs dürfen NIRGENDS kundenseitig erscheinen
# (Ali-Anweisung 2026-05-29). Vor jeder Auslieferung ans Frontend (HTML-Bootstrap
# window.__HOTELS__ + /api/search) werden sie aus dem Hotel-Objekt entfernt.
_HOTEL_INTERNAL_KEYS = ("bilyana_oid", "golfbelek_id")


def _hotel_public(h: dict) -> dict:
    return {k: v for k, v in h.items() if k not in _HOTEL_INTERNAL_KEYS}


GOLFBELEK_BOOTSTRAP_URL = "https://golfbelek.com/gp/server/api/bootstrap.php"
_gb_cache: dict = {"data": None, "fetched": 0.0}
_GB_TTL_SECONDS = 60  # live: 1 Minute Cache, dann Refresh

# --- Playwright Browser-Pool für Live-Scraping ---
_playwright_state: dict = {"pw": None, "browser": None, "lock": None}
_scrape_cache: dict = {}
_SCRAPE_TTL = 90  # 90 Sekunden Live-Cache pro Such-Kombi


async def _get_browser() -> Browser:
    """Lazy-init shared headless Chromium browser."""
    if _playwright_state.get("lock") is None:
        _playwright_state["lock"] = asyncio.Lock()
    async with _playwright_state["lock"]:
        browser = _playwright_state.get("browser")
        if browser is not None:
            try:
                if browser.is_connected():
                    return browser
            except Exception:
                pass
        pw = _playwright_state.get("pw")
        if pw is None:
            pw = await async_playwright().start()
            _playwright_state["pw"] = pw
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        _playwright_state["browser"] = browser
        return browser


async def _scrape_golfbelek(gb_hotel_id: int, iso_date: str, nights: int, rounds_val) -> Optional[dict]:
    """Lädt golfbelek.com /gp/en/ mit den Query-Params, scrappt den real gerenderten Preis."""
    key = f"{gb_hotel_id}|{iso_date}|{nights}|{rounds_val}"
    now = time.time()
    cached = _scrape_cache.get(key)
    if cached and now - cached["t"] < _SCRAPE_TTL:
        return cached["v"]

    if str(rounds_val).upper() == "UNLIMITED":
        rounds_param = "unlimited"
    else:
        rounds_param = str(rounds_val)

    url = (
        f"https://golfbelek.com/gp/en/"
        f"?hotel_id={gb_hotel_id}&checkin={iso_date}&nights={nights}&rounds={rounds_param}"
    )

    browser = await _get_browser()
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        try:
            # Warte primär auf das echte Preis-Element; nach Timeout still weiter.
            await page.wait_for_selector(
                ".package-card__price-block .pp",
                timeout=18000,
            )
        except Exception:
            try:
                # Wenn kein Preis-Element kommt, könnte ein "no results"-Zustand vorliegen.
                await page.wait_for_selector(".no-results, .package-card", timeout=4000)
            except Exception:
                pass
        await asyncio.sleep(1.5)
        html = await page.content()
    finally:
        await context.close()

    result = _parse_golfbelek_rendered(html, nights, rounds_val)
    _scrape_cache[key] = {"t": now, "v": result}
    return result


def _parse_golfbelek_rendered(html: str, nights_req: int, rounds_req) -> Optional[dict]:
    """Parse den gerenderten golfbelek-DOM: erstes package-card mit Preis."""
    soup = BeautifulSoup(html, "html.parser")
    card = soup.select_one(".package-card")
    if not card:
        return None

    course_title_el = card.select_one(".course-title__link") or card.select_one(".course-title")
    hotel_name = course_title_el.get_text(" ", strip=True) if course_title_el else ""

    sub_el = card.select(".sub")
    board_room = sub_el[0].get_text(" ", strip=True) if len(sub_el) > 0 else ""
    check_in_out = sub_el[1].get_text(" ", strip=True) if len(sub_el) > 1 else ""
    courses_inc = sub_el[2].get_text(" ", strip=True) if len(sub_el) > 2 else ""

    timechips = [c.get_text(" ", strip=True) for c in card.select(".timechips .timechip")]

    pp_el = card.select_one(".package-card__price-block .pp")
    pp_text = pp_el.get_text(" ", strip=True) if pp_el else ""

    total_el = card.select_one(".package-card__price-block .tot")
    total_text = total_el.get_text(" ", strip=True) if total_el else ""

    fallback_banner = ""
    banner = soup.select_one("#nightsRoundsBanner")
    if banner:
        fallback_banner = banner.get_text(" ", strip=True)

    pp = _extract_price(pp_text)
    if not pp:
        return None

    total = _extract_price(total_text) if total_text else None

    return {
        "hotel_name_remote": hotel_name,
        "board_room": board_room,
        "check_in_out": check_in_out,
        "courses_included": courses_inc,
        "stay_chips": timechips,
        "fallback_banner": fallback_banner,
        "price_per_golfer": pp,
        "price_total": total,
    }


def db_init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            hotel_oid INTEGER,
            hotel_name TEXT,
            travel_date TEXT,
            nights TEXT,
            rounds TEXT,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            note TEXT,
            extra_rounds TEXT,
            club_hire INTEGER DEFAULT 0,
            user_agent TEXT
        )
    """)
    # additive migrations for older DB files
    for col, ddl in [
        ("extra_rounds", "ALTER TABLE inquiries ADD COLUMN extra_rounds TEXT"),
        ("club_hire",    "ALTER TABLE inquiries ADD COLUMN club_hire INTEGER DEFAULT 0"),
        ("golfers",      "ALTER TABLE inquiries ADD COLUMN golfers INTEGER DEFAULT 2"),
        ("non_golfers",  "ALTER TABLE inquiries ADD COLUMN non_golfers INTEGER DEFAULT 0"),
        ("source",       "ALTER TABLE inquiries ADD COLUMN source TEXT"),
        ("price",        "ALTER TABLE inquiries ADD COLUMN price TEXT"),
    ]:
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass
    con.execute("""
        CREATE TABLE IF NOT EXISTS proxy_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            saved_at INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()


db_init()

app = FastAPI(title="Golf Search Belek", version="1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _cache_get(key: str, ttl_seconds: int = 300) -> Optional[dict]:
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT payload, saved_at FROM proxy_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    payload, saved_at = row
    if time.time() - saved_at > ttl_seconds:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def _cache_put(key: str, value: dict) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "INSERT OR REPLACE INTO proxy_cache(cache_key, payload, saved_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), int(time.time())),
        )
        con.commit()
    finally:
        con.close()


async def _fetch(path: str, params: dict, timeout: float = 25.0) -> str:
    url = BILYANA_DOMAIN + path.lstrip("/")
    headers = {"User-Agent": USER_AGENT, "Referer": BILYANA_DOMAIN}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.text


async def _golfbelek_data() -> dict:
    """Bootstrap-Data live von golfbelek (max 60s alt)."""
    now = time.time()
    if _gb_cache["data"] is not None and now - _gb_cache["fetched"] < _GB_TTL_SECONDS:
        return _gb_cache["data"]
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://golfbelek.com/",
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(GOLFBELEK_BOOTSTRAP_URL, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    _gb_cache["data"] = data
    _gb_cache["fetched"] = now
    return data


def _iso_date_from_de(de_date: str) -> Optional[str]:
    """'24-05-2026' -> '2026-05-24'."""
    m = re.match(r"^(\d{2})[-./](\d{2})[-./](\d{4})$", (de_date or "").strip())
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def _golfbelek_nights_rounds(data: dict, gb_hotel_id: int, iso_date: Optional[str] = None) -> dict:
    """Liefere die Nights/Rounds-Optionen des Hotels.

    Mit iso_date: nur Optionen aus Perioden, die dieses Datum abdecken, exakt
    wie golfbelek's Datums-Filter (`filterBaseCandidates()`). Ohne iso_date:
    alle Optionen aus allen Paketen als ungefilterter Fallback.
    """
    packages_by_id = {
        p["id"]: p for p in data.get("golfPackages", []) if p.get("hotelId") == gb_hotel_id
    }
    if not packages_by_id:
        return {"nights": [], "rounds": [], "has_unlimited": False}

    rounds_set: set = set()
    nights_set: set = set()
    has_unlimited = False

    if iso_date:
        # Datums-gefiltert: nur Perioden, die iso_date abdecken.
        periods_by_id = {
            p["id"]: p for p in data.get("periods", []) if p.get("hotelId") == gb_hotel_id
        }
        for pr in data.get("golfPrices", []):
            if pr.get("hotelId") != gb_hotel_id:
                continue
            period = periods_by_id.get(pr.get("periodId"))
            if not period or not period.get("start") or not period.get("end"):
                continue
            if not (period["start"] <= iso_date <= period["end"]):
                continue
            if pr.get("nights"):
                try:
                    nights_set.add(int(pr["nights"]))
                except (TypeError, ValueError):
                    pass
            if int(pr.get("unlimited") or 0) == 1:
                has_unlimited = True
            pkg = packages_by_id.get(pr.get("packageId"))
            if pkg:
                r = pkg.get("rounds")
                if r is not None and r > 0:
                    if r >= 99:
                        has_unlimited = True
                    else:
                        rounds_set.add(int(r))
        return {
            "nights": sorted(nights_set),
            "rounds": sorted(rounds_set),
            "has_unlimited": has_unlimited,
        }

    # Ungefiltert (noch kein Datum gewählt): alle Pakete/Preise.
    for p in packages_by_id.values():
        r = p.get("rounds")
        if r is not None and r > 0:
            # golfbelek listet auch hohe Rounds (8/15/20) von "Unlimited-Golf"-Paketen
            # als reguläre Optionen. Nur reine ≥99-Marker zählen als Wildcard-Unlimited.
            if r >= 99:
                has_unlimited = True
            else:
                rounds_set.add(int(r))
        m = re.match(r"^\s*(\d+)\s*[Nn]", p.get("name") or "")
        if m:
            try:
                nights_set.add(int(m.group(1)))
            except ValueError:
                pass

    for pr in data.get("golfPrices", []):
        if pr.get("hotelId") != gb_hotel_id:
            continue
        if int(pr.get("unlimited") or 0) == 1:
            has_unlimited = True
        if pr.get("nights"):
            try:
                nights_set.add(int(pr["nights"]))
            except (TypeError, ValueError):
                pass

    return {
        "nights": sorted(nights_set),
        "rounds": sorted(rounds_set),
        "has_unlimited": has_unlimited,
    }


def _gb_per_person_with_margins(pr: dict, pkg: Optional[dict], is_golfer: bool) -> float:
    """Nachbau von golfbelek's perPersonWithMargins()."""
    base_val = float(pr.get("gd") or pr.get("gs") or 0) if is_golfer else float(pr.get("ngd") or pr.get("ngs") or 0)
    marj_night = float(pr.get("marjNight") or 0)
    marj_course = float(pr.get("marjCourse") or 0)
    marj_x = float(pr.get("marjX") or 0)
    nights = int(pr.get("nights") or 1)
    rounds_pkg = int((pkg or {}).get("rounds") or 0)
    nights_adj = marj_night * nights
    rounds_adj = marj_course * (rounds_pkg if is_golfer else 0)
    return base_val + nights_adj + rounds_adj + marj_x


def _gb_apply_hot_deal(price: float, hot_deal: Optional[dict], nights: int) -> float:
    if not hot_deal:
        return price
    rule = hot_deal.get("pricerule")
    if rule == "percent" and hot_deal.get("discPercent") is not None:
        return price * (1 - float(hot_deal["discPercent"]) / 100.0)
    if rule == "night" and hot_deal.get("discNight") is not None:
        d = float(hot_deal["discNight"]) * nights
        return max(0.0, price - d)
    return price


def _gb_active_hot_deal(data: dict, hotel_id: int, room_type_id: int, iso_date: str) -> Optional[dict]:
    for hd in data.get("hotDeals", []):
        if hd.get("hotelId") != hotel_id:
            continue
        if hd.get("roomTypeId") != room_type_id:
            continue
        if hd.get("start") and hd.get("end") and hd["start"] <= iso_date <= hd["end"]:
            return hd
    return None


def _gb_in_stop_sale(data: dict, hotel_id: int, room_type_id: int, iso_date: str) -> bool:
    for ss in data.get("stopSales", []):
        if ss.get("hotelId") != hotel_id:
            continue
        if ss.get("roomTypeId") != room_type_id:
            continue
        if ss.get("start") and ss.get("end") and ss["start"] <= iso_date <= ss["end"]:
            return True
    return False


def _golfbelek_search(
    data: dict,
    gb_hotel_id: int,
    iso_date: str,
    nights: int,
    rounds_val,
) -> Optional[dict]:
    """Live-Lookup mit Nights/Rounds-Fallback + Marginal-Berechnung.

    Wie golfbelek's `filterBaseCandidates()` + `nightsFallbackValue()` +
    `roundsFallbackValue()` + `perPersonWithMargins()` in deren `gp/en` Page.
    """
    packages_by_id = {p["id"]: p for p in data.get("golfPackages", []) if p.get("hotelId") == gb_hotel_id}
    if not packages_by_id:
        return None
    periods_by_id = {p["id"]: p for p in data.get("periods", []) if p.get("hotelId") == gb_hotel_id}
    if not periods_by_id:
        return None

    requested_unlimited = (
        rounds_val == "unlimited"
        or rounds_val == "UNLIMITED"
        or (isinstance(rounds_val, int) and rounds_val >= 99)
    )

    candidates: list[tuple[dict, dict, dict]] = []
    for pr in data.get("golfPrices", []):
        if pr.get("hotelId") != gb_hotel_id:
            continue
        period = periods_by_id.get(pr.get("periodId"))
        if not period or not period.get("start") or not period.get("end"):
            continue
        if not (period["start"] <= iso_date <= period["end"]):
            continue
        pkg = packages_by_id.get(pr.get("packageId"))
        if not pkg:
            continue
        is_unl = int(pr.get("unlimited") or 0) == 1
        if requested_unlimited and not is_unl:
            continue
        if not requested_unlimited and is_unl:
            continue
        if _gb_in_stop_sale(data, gb_hotel_id, pr.get("roomTypeId"), iso_date):
            continue
        candidates.append((pr, pkg, period))

    if not candidates:
        return None

    available_nights = sorted({int(c[0].get("nights") or 0) for c in candidates if c[0].get("nights")})
    eff_nights = nights
    nights_fallback = False
    if nights not in available_nights:
        if not available_nights:
            return None
        nights_fallback = True
        eff_nights = min(available_nights, key=lambda v: (abs(v - nights), v))

    nights_pool = [c for c in candidates if int(c[0].get("nights") or 0) == eff_nights]
    if not nights_pool:
        return None

    eff_rounds_val: object = rounds_val
    rounds_fallback = False
    if not requested_unlimited:
        try:
            r_int = int(rounds_val)
        except (TypeError, ValueError):
            return None
        available_rounds = sorted({int((c[1].get("rounds") or 0)) for c in nights_pool if c[1].get("rounds")})
        if r_int not in available_rounds:
            if not available_rounds:
                return None
            rounds_fallback = True
            r_int = min(available_rounds, key=lambda v: (abs(v - r_int), v))
        eff_rounds_val = r_int
        final_pool = [c for c in nights_pool if int(c[1].get("rounds") or 0) == r_int]
    else:
        final_pool = nights_pool

    if not final_pool:
        return None

    best_price = None
    best_meta = None
    for pr, pkg, period in final_pool:
        price = _gb_per_person_with_margins(pr, pkg, is_golfer=True)
        if price <= 0:
            continue
        hot_deal = _gb_active_hot_deal(data, gb_hotel_id, pr.get("roomTypeId"), iso_date)
        if hot_deal:
            price = _gb_apply_hot_deal(price, hot_deal, int(pr.get("nights") or 0))
        if best_price is None or price < best_price:
            best_price = price
            best_meta = (pr, pkg, period, hot_deal)

    if best_price is None or best_meta is None:
        return None

    pr, pkg, period, hot_deal = best_meta
    return {
        "label": "Per Person",
        "price_actual": {
            "currency": "EUR",
            "amount": float(best_price),
            "display": f"€ {int(round(best_price))}",
        },
        "price_original": None,
        "source": "golfbelek.com",
        "package_name": pkg.get("name") if pkg else None,
        "period": {"start": period["start"], "end": period["end"]} if period else None,
        "board": pr.get("board") or "",
        "eff_nights": eff_nights,
        "eff_rounds": eff_rounds_val,
        "nights_fallback": nights_fallback,
        "rounds_fallback": rounds_fallback,
        "hot_deal_applied": bool(hot_deal),
    }


def _merged_nights_rounds(hotel: dict, bilyana_nights: list, bilyana_rounds: list, gb_options: dict) -> dict:
    """Union aus Bilyana + Golfbelek Optionen. Labels werden sauber in Englisch
    neu aufgebaut (rein aus dem Integer-Wert), damit beide Quellen identisch
    formatiert sind und kein Doppel-Leerzeichen-Bug aus Bilyana durchschlägt."""
    nights_set: set[int] = set()
    for opt in bilyana_nights:
        try:
            nights_set.add(int(opt["value"]))
        except (ValueError, KeyError, TypeError):
            continue
    for n in gb_options.get("nights", []):
        nights_set.add(n)
    nights_options = [
        {"value": str(n), "label": f"{n} Night{'s' if n != 1 else ''}"}
        for n in sorted(nights_set)
    ]

    rounds_set: set[int] = set()
    has_unlimited = gb_options.get("has_unlimited", False)
    for opt in bilyana_rounds:
        try:
            r = int(opt["value"])
            if r >= 99:
                has_unlimited = True
            else:
                rounds_set.add(r)
        except (ValueError, KeyError, TypeError):
            continue
    for r in gb_options.get("rounds", []):
        rounds_set.add(r)
    rounds_options = [
        {"value": str(r), "label": f"{r} Round{'s' if r != 1 else ''}"}
        for r in sorted(rounds_set)
    ]
    if has_unlimited:
        rounds_options.append({"value": "UNLIMITED", "label": "Unlimited Rounds"})
    return {"nights": nights_options, "rounds": rounds_options}


def _parse_options(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for opt in soup.find_all("option"):
        val = (opt.get("value") or "").strip()
        label = opt.get_text(strip=True)
        if not val or val == "0" or val == "-1":
            continue
        out.append({"value": val, "label": label})
    return out


_RE_PRICE_PRE = re.compile(
    r"(?P<currency>[€£$]|EUR|USD|GBP|CHF)\s*(?P<amount>\d[\d\.\,]*)",
    re.IGNORECASE,
)
_RE_PRICE_POST = re.compile(
    r"(?P<amount>\d[\d\.\,]*)\s*(?P<currency>[€£$]|EUR|USD|GBP|CHF)",
    re.IGNORECASE,
)
_CURRENCY_MAP = {"€": "EUR", "£": "GBP", "$": "USD"}


def _extract_price(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _RE_PRICE_PRE.search(text) or _RE_PRICE_POST.search(text)
    if not m:
        return None
    raw = m.group("amount")
    cur_raw = m.group("currency")
    cur = _CURRENCY_MAP.get(cur_raw, cur_raw.upper())
    cleaned = raw.replace(".", "").replace(",", ".")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0:
        return None
    # Original-Format aus dem HTML beibehalten (entweder "€ 1.095" oder "3.793 €")
    return {"currency": cur, "amount": amount, "display": m.group(0).strip()}


def _is_strikethrough(el) -> bool:
    """Span gilt als durchgestrichen wenn style line-through hat oder Klasse text-danger."""
    style = (el.get("style") or "").lower()
    if "line-through" in style:
        return True
    classes = el.get("class") or []
    if "text-danger" in classes:
        return True
    return False


def _parse_price_column(col) -> Optional[dict]:
    """Eine .col-md-6 Preis-Spalte parsen.

    Liefert {label, price_actual, price_original} oder None.
    label = 'Per Person of Group' | 'Per Person' | ...
    price_actual = aktueller Preis (nicht durchgestrichen)
    price_original = durchgestrichener Original-Preis (optional)
    """
    text_center = col.select_one(".text-center")
    if not text_center:
        return None

    label = ""
    for node in text_center.contents:
        if isinstance(node, str):
            chunk = node.strip()
            if chunk:
                label = chunk
                break
        else:
            name = getattr(node, "name", "")
            if name in {"br", "i"}:
                continue
            txt = node.get_text(" ", strip=True)
            if txt:
                label = txt
                break
    label = re.sub(r"\s+", " ", label).strip(": ")
    if not label:
        label = "Per Person"

    price_actual = None
    price_original = None
    for span in text_center.find_all("span"):
        text = span.get_text(" ", strip=True)
        if not text:
            continue
        extracted = _extract_price(text)
        if not extracted:
            continue
        if _is_strikethrough(span):
            if price_original is None:
                price_original = extracted
        else:
            classes = span.get("class") or []
            has_dark = "text-dark" in classes
            has_strong_weight = any(c in classes for c in ("font-weight-bold", "font-weight-bolder"))
            style = (span.get("style") or "").lower()
            is_big = "font-size:21" in style or "font-size: 21" in style
            if has_dark or has_strong_weight or is_big:
                if price_actual is None or (price_actual["amount"] != extracted["amount"]):
                    price_actual = extracted

    if price_actual is None:
        for span in text_center.find_all("span"):
            if _is_strikethrough(span):
                continue
            if "discount" in (span.get("class") or []):
                continue
            extracted = _extract_price(span.get_text(" ", strip=True))
            if extracted:
                price_actual = extracted
                break

    if not price_actual:
        return None
    return {
        "label": label,
        "price_actual": price_actual,
        "price_original": price_original,
    }


def _parse_packages(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    packages: list[dict] = []
    for card in soup.select(".OtlPkgCard1"):
        header = card.select_one(".card-header .btn1")
        header_text = header.get_text(" ", strip=True) if header else ""
        date_span = header.select_one("span") if header else None
        date_range = date_span.get_text(strip=True) if date_span else ""
        nights_rounds = ""
        if header_text:
            cleaned = header_text.replace(date_range, "").strip()
            nights_rounds = re.sub(r"\s+", " ", cleaned).strip(" >»")
            nights_rounds = nights_rounds.replace("&nbsp;", " ").strip()

        bullets: list[str] = []
        for ul in card.select(".card-body ul"):
            for li in ul.find_all("li"):
                txt = li.get_text(" ", strip=True)
                if not txt or "Bilyana Rewards" in txt or "Strokes" in txt:
                    continue
                bullets.append(re.sub(r"\s+", " ", txt))

        promo_badges: list[str] = []
        for badge in card.select(".oci-card-group1 span.text-uppercase"):
            txt = badge.get_text(" ", strip=True)
            if txt and txt not in promo_badges:
                promo_badges.append(txt)

        price_options: list[dict] = []
        for col in card.select(".card-body .col-md-6"):
            classes = " ".join(col.get("class") or [])
            if "hrc-features-price2" not in classes and "font-weight-bolder1" not in classes:
                continue
            parsed = _parse_price_column(col)
            if parsed:
                price_options.append(parsed)

        if not price_options:
            for tc in card.select(".card-body .text-center"):
                wrapper_col = tc.find_parent("div", class_="col-md-6")
                fake = _parse_price_column(wrapper_col or tc)
                if fake:
                    price_options.append(fake)

        primary = None
        for opt in price_options:
            label_low = (opt["label"] or "").lower()
            if "per person" in label_low and "group" not in label_low:
                primary = opt
                break
        if primary is None and price_options:
            primary = price_options[0]

        packages.append(
            {
                "nights_rounds": nights_rounds,
                "date_range": date_range,
                "bullets": bullets,
                "promo_badges": promo_badges,
                "price_options": price_options,
                "primary_price": primary,
            }
        )
    return packages


async def _bilyana_options(endpoint: str, bilyana_oid: int, rounds: str, nights: str, board: str, tdate: str) -> list[dict]:
    """endpoint = 'night_getir' oder 'round_getir'."""
    html = await _fetch(
        f"_layouts/hotel/{endpoint}.php",
        {
            "host": BILYANA_DOMAIN,
            "otel_id": bilyana_oid,
            "round": rounds,
            "night": nights,
            "board": board,
            "tdate": tdate,
        },
    )
    return _parse_options(html)


@app.get("/api/hotels")
async def api_hotels():
    out = []
    for h in BELEK_HOTELS:
        sources = []
        if h.get("bilyana_oid"):
            sources.append("bilyana")
        if h.get("golfbelek_id"):
            sources.append("golfbelek")
        out.append({**_hotel_with_images(h), "sources": sources})
    return {"hotels": out}


@app.get("/api/nights")
async def api_nights(hotel_oid: int, rounds: str = "", board: str = "", tdate: str = ""):
    if hotel_oid not in HOTEL_BY_OID:
        raise HTTPException(404, "unknown hotel")
    hotel = HOTEL_BY_OID[hotel_oid]
    # Nächte exakt für das gewählte Datum (genau wie Bilyana + Golfbelek live anzeigen).
    iso_date = _iso_date_from_de(tdate) if tdate else None
    bilyana_options: list = []
    if hotel.get("bilyana_oid"):
        try:
            bilyana_options = await _bilyana_options(
                "night_getir", hotel["bilyana_oid"], rounds, "", board, tdate,
            )
        except Exception:
            bilyana_options = []
    gb_options: dict = {"nights": [], "rounds": [], "has_unlimited": False}
    if hotel.get("golfbelek_id"):
        try:
            data = await _golfbelek_data()
            gb_options = _golfbelek_nights_rounds(data, hotel["golfbelek_id"], iso_date)
        except Exception:
            pass
    merged = _merged_nights_rounds(hotel, bilyana_options, [], gb_options)
    return {"options": merged["nights"]}


@app.get("/api/rounds")
async def api_rounds(hotel_oid: int, nights: str = "", board: str = "", tdate: str = ""):
    if hotel_oid not in HOTEL_BY_OID:
        raise HTTPException(404, "unknown hotel")
    hotel = HOTEL_BY_OID[hotel_oid]
    # Runden exakt für das gewählte Datum (genau wie Bilyana + Golfbelek live anzeigen).
    iso_date = _iso_date_from_de(tdate) if tdate else None
    bilyana_options: list = []
    if hotel.get("bilyana_oid"):
        try:
            bilyana_options = await _bilyana_options(
                "round_getir", hotel["bilyana_oid"], "", nights, board, tdate,
            )
        except Exception:
            bilyana_options = []
    gb_options: dict = {"nights": [], "rounds": [], "has_unlimited": False}
    if hotel.get("golfbelek_id"):
        try:
            data = await _golfbelek_data()
            gb_options = _golfbelek_nights_rounds(data, hotel["golfbelek_id"], iso_date)
        except Exception:
            pass
    merged = _merged_nights_rounds(hotel, [], bilyana_options, gb_options)
    return {"options": merged["rounds"]}


@app.get("/api/search")
async def api_search(
    hotel_oid: int,
    travel_date: str = "",
    nights: str = "",
    rounds: str = "",
    board: str = "",
    currency: str = "EUR",
):
    if hotel_oid not in HOTEL_BY_OID:
        raise HTTPException(404, "unknown hotel")
    hotel = HOTEL_BY_OID[hotel_oid]
    sources_tried: list[str] = []

    # Beide Quellen GLEICHZEITIG live abfragen (Bilyana + Golfbelek parallel),
    # damit BEIDE Preise nebeneinander gezeigt werden (Ali-Anweisung 2026-05-29).
    async def _get_bilyana() -> list[dict]:
        if not hotel.get("bilyana_oid"):
            return []
        try:
            html = await _fetch(
                "_layouts/hotel/offer_seasons_ajax.php",
                {
                    "host": BILYANA_DOMAIN,
                    "otel_id": hotel["bilyana_oid"],
                    "night": nights,
                    "round": rounds,
                    "board": board,
                    "tdate": travel_date,
                    "cagri": "0",
                    "sort": "",
                    "pkgCurr": currency,
                },
            )
            pkgs = _parse_packages(html)
            for p in pkgs:
                if p.get("price_options"):
                    p["source"] = "bilyana"
            return pkgs
        except Exception:
            return []

    async def _get_golfbelek() -> list[dict]:
        if not hotel.get("golfbelek_id"):
            return []
        iso_date = _iso_date_from_de(travel_date)
        if not (iso_date and nights):
            return []
        try:
            n_int = int(nights)
        except ValueError:
            return []
        if n_int <= 0:
            return []
        rounds_param: object = rounds
        if rounds and rounds.upper() != "UNLIMITED":
            try:
                rounds_param = int(rounds)
            except ValueError:
                rounds_param = rounds
        try:
            scraped = await _scrape_golfbelek(hotel["golfbelek_id"], iso_date, n_int, rounds_param)
        except Exception:
            return []
        if not (scraped and scraped.get("price_per_golfer")):
            return []
        pp = scraped["price_per_golfer"]
        total = scraped.get("price_total")
        bullets: list[str] = []
        if scraped.get("board_room"):
            bullets.append(scraped["board_room"])
        if scraped.get("check_in_out"):
            bullets.append(scraped["check_in_out"])
        if scraped.get("courses_included"):
            bullets.append(scraped["courses_included"])
        if scraped.get("fallback_banner"):
            bullets.append(scraped["fallback_banner"])
        rounds_disp = "Unlimited" if str(rounds_param).lower() == "unlimited" else str(rounds_param)
        price_opt = {
            "label": "Per golfer",
            "price_actual": pp,
            "price_original": None,
            "total": total,
        }
        return [{
            "nights_rounds": f"{n_int} Night{'s' if n_int != 1 else ''} · {rounds_disp} Round{'s' if str(rounds_disp) != '1' else ''}",
            "date_range": scraped.get("check_in_out") or "",
            "bullets": [b for b in bullets if b],
            "promo_badges": [],
            "price_options": [price_opt],
            "primary_price": dict(price_opt),
            "source": "golfbelek",
        }]

    if hotel.get("bilyana_oid"):
        sources_tried.append("bilyana")
    if hotel.get("golfbelek_id"):
        sources_tried.append("golfbelek")

    bilyana_pkgs, golfbelek_pkgs = await asyncio.gather(_get_bilyana(), _get_golfbelek())

    # Preis-Konsolidierung (Ali-Anweisung 2026-05-29):
    # Aus BEIDEN Quellen den günstigsten Per-Person-Preis nehmen, dann IMMER
    # 5% günstiger anzeigen (Reseller-Marge). Es wird genau EINE Karte gebaut.
    # Die Inklusivleistungen (Bullets/Promo) kommen ausschließlich von Bilyana.
    bilyana_priced = [p for p in bilyana_pkgs if p.get("price_options")]
    golfbelek_priced = [p for p in golfbelek_pkgs if p.get("price_options")]

    def _pp_amount(pkg: dict) -> Optional[float]:
        pp = pkg.get("primary_price") or {}
        pa = pp.get("price_actual") or {}
        amt = pa.get("amount") if isinstance(pa, dict) else None
        return float(amt) if isinstance(amt, (int, float)) and amt > 0 else None

    candidates: list[tuple] = []
    for p in bilyana_priced:
        amt = _pp_amount(p)
        if amt is not None:
            candidates.append(("bilyana", amt, p))
    for p in golfbelek_priced:
        amt = _pp_amount(p)
        if amt is not None:
            candidates.append(("golfbelek", amt, p))

    packages: list[dict] = []
    primary_source: Optional[str] = None

    if candidates:
        best_source, best_amount, best_pkg = min(candidates, key=lambda c: c[1])
        primary_source = best_source
        best_pa = (best_pkg.get("primary_price") or {}).get("price_actual") or {}
        cur = best_pa.get("currency") or currency or "EUR"
        sym = {"EUR": "€", "GBP": "£", "USD": "$"}.get(cur, cur)

        def _fmt(a: float) -> str:
            return f"{sym} {int(round(a)):,}"

        our_amount = round(best_amount * (1.0 - RESELLER_DISCOUNT))
        # Inklusivleistungen ausschließlich aus Bilyana, sonst Fallback bestes Paket.
        incl_pkg = bilyana_priced[0] if bilyana_priced else best_pkg

        price_opt = {
            "label": "Per person",
            "price_actual": {
                "currency": cur,
                "amount": float(our_amount),
                "display": _fmt(our_amount),
            },
            "price_original": {
                "currency": cur,
                "amount": float(round(best_amount)),
                "display": _fmt(best_amount),
            },
            "total": None,
        }

        try:
            n_disp = int(nights)
            nr_n = f"{n_disp} Night{'s' if n_disp != 1 else ''}"
        except ValueError:
            nr_n = f"{nights} Nights"
        if (rounds or "").upper() == "UNLIMITED":
            nr_r = "Unlimited Rounds"
        else:
            try:
                r_disp = int(rounds)
                nr_r = f"{r_disp} Round{'s' if r_disp != 1 else ''}"
            except ValueError:
                nr_r = f"{rounds} Rounds"

        packages = [{
            "nights_rounds": f"{nr_n} · {nr_r}",
            "date_range": incl_pkg.get("date_range") or best_pkg.get("date_range") or "",
            "bullets": incl_pkg.get("bullets") or best_pkg.get("bullets") or [],
            "promo_badges": incl_pkg.get("promo_badges") or [],
            "price_options": [price_opt],
            "primary_price": dict(price_opt),
            "source": _SOURCE_CODE.get(best_source, ""),
        }]

    has_price = bool(packages)
    return {
        "hotel": _hotel_public(_hotel_with_images(hotel)),
        "query": {
            "travel_date": travel_date,
            "nights": nights,
            "rounds": rounds,
            "board": board,
            "currency": currency,
        },
        "packages": packages,
        "has_price": has_price,
        "package_count": len(packages),
    }


@app.post("/api/inquiry")
async def api_inquiry(request: Request):
    form = await request.form()
    try:
        hotel_oid = int(form.get("hotel_oid") or 0)
    except ValueError:
        raise HTTPException(400, "hotel_oid invalid")
    if hotel_oid not in HOTEL_BY_OID:
        raise HTTPException(404, "unknown hotel")
    hotel = HOTEL_BY_OID[hotel_oid]
    full_name = (str(form.get("full_name") or "")).strip()
    email = (str(form.get("email") or "")).strip()
    phone = (str(form.get("phone") or "")).strip()
    note = str(form.get("note") or "")
    travel_date = str(form.get("travel_date") or "")
    nights = str(form.get("nights") or "")
    rounds = str(form.get("rounds") or "")
    # Neutraler Code vom Frontend zurück auf echten Operator-Namen mappen
    # (nur für Alis internes DB-Audit, nie kundenseitig sichtbar).
    source_code = str(form.get("source") or "")
    source = _SOURCE_FROM_CODE.get(source_code, source_code)
    price = str(form.get("price") or "")
    club_hire = 1 if str(form.get("club_hire") or "").lower() in ("on", "1", "true", "yes") else 0
    # Accept array via getlist (multiple checkboxes name="extra_rounds")
    extra_rounds_list = [str(v) for v in form.getlist("extra_rounds") if str(v).strip()]
    extra_rounds_text = "\n".join(extra_rounds_list)
    try:
        golfers = max(0, min(50, int(form.get("golfers") or 0)))
    except (TypeError, ValueError):
        golfers = 0
    try:
        non_golfers = max(0, min(50, int(form.get("non_golfers") or 0)))
    except (TypeError, ValueError):
        non_golfers = 0

    if not full_name or not email or not phone:
        raise HTTPException(400, "name, email and phone are required")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "invalid email")

    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            """
            INSERT INTO inquiries
                (hotel_oid, hotel_name, travel_date, nights, rounds,
                 full_name, email, phone, note, extra_rounds, club_hire,
                 golfers, non_golfers, source, price, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hotel_oid,
                hotel["name"],
                travel_date,
                nights,
                rounds,
                full_name,
                email,
                phone,
                note,
                extra_rounds_text,
                club_hire,
                golfers,
                non_golfers,
                source,
                price,
                request.headers.get("user-agent", ""),
            ),
        )
        con.commit()
        inquiry_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        con.close()
    return {"ok": True, "inquiry_id": inquiry_id}


def _asset_version() -> str:
    """Cache-Buster: neuster mtime von app.js + style.css, damit der Browser nach
    jedem Deploy garantiert die frische Datei laedt und keine alte Version cached."""
    latest = 0.0
    for name in ("app.js", "style.css"):
        try:
            latest = max(latest, (BASE_DIR / "static" / name).stat().st_mtime)
        except OSError:
            pass
    return str(int(latest))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    default_date = (datetime.utcnow() + timedelta(days=14)).strftime("%d-%m-%Y")
    hotels = [_hotel_public(_hotel_with_images(h)) for h in BELEK_HOTELS]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "hotels": hotels,
            "hotels_json": json.dumps(hotels, ensure_ascii=False),
            "courses_json": json.dumps(EXTRA_GOLF_COURSES, ensure_ascii=False),
            "default_date": default_date,
            "extra_courses": EXTRA_GOLF_COURSES,
            "asset_v": _asset_version(),
        },
    )


@app.get("/health")
async def health():
    return {"ok": True, "service": "golf-search", "hotels": len(BELEK_HOTELS)}


@app.on_event("shutdown")
async def _shutdown():
    browser = _playwright_state.get("browser")
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    pw = _playwright_state.get("pw")
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8917, reload=False)
