#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "playwright>=1.45",
#   "tzdata; sys_platform == 'win32'",
# ]
# ///
"""
utcourts — check open courts on UT RecSports (apps.rs.utexas.edu) from the terminal.

  utcourts login                         sign in once (opens a browser window)
  utcourts check                         every facility, today
  utcourts check squash tomorrow         one sport, one day
  utcourts check rb fri --after 6pm      racquetball/handball, Friday evening
  utcourts check tennis --days 3 --json  next 3 days, machine-readable
  utcourts check gre-squash --grid       full grid for one facility
  utcourts facilities                    list facilities and their short names
  utcourts status                        is the saved session still good?

How the "don't make me log in all the time" part works
------------------------------------------------------
* `login` opens a real browser window with its own private profile (kept in
  ~/.config/utcourts/profile). You sign in with your UT EID + Duo once.
* The cookies from that profile are saved to ~/.config/utcourts/session.json
  (readable only by you) and normal checks are plain HTTP requests with those
  cookies — fast, no browser.
* When the RecSports session runs out, the tool quietly reopens the same
  profile in a hidden browser. If UT's single sign-on still remembers you, it
  logs back in on its own and the check just continues. You only get asked
  to sign in again when UT's SSO / Duo has fully expired.

Exit codes: 0 ok · 1 usage/other error · 2 sign-in needed · 3 page changed / network trouble
"""
from __future__ import annotations

__version__ = "1.0.0"

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

try:
    from zoneinfo import ZoneInfo
    AUSTIN_TZ = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - tzdata missing (e.g. bare Windows)
    AUSTIN_TZ = None

import httpx
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Site constants
# --------------------------------------------------------------------------- #
BASE_URL = os.environ.get("UTCOURTS_BASE_URL", "https://apps.rs.utexas.edu")
COURTS_PATH = "/app/myrecsports/reserve_courts.php"
COURTS_URL = BASE_URL + COURTS_PATH
APP_PREFIX = "/app/myrecsports/"
MINE_URL = BASE_URL + APP_PREFIX + "myreservations.php"
APP_HOST = urlparse(BASE_URL).hostname
MAX_DAYS_AHEAD = 7  # the site's date picker allows today .. today + 1 week

HOME = Path(os.environ.get("UTCOURTS_HOME")
            or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "utcourts")
SESSION_FILE = HOME / "session.json"
PROFILE_DIR = HOME / "profile"

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


@dataclass(frozen=True)
class Facility:
    id: int
    sports: tuple[str, ...]
    name: str
    aliases: tuple[str, ...]


FACILITIES: list[Facility] = [
    Facility(28, ("squash",), "Bellmont Hall (BEL) - Squash", ("bel-squash", "bel")),
    Facility(30, ("pickleball",), "Caven-Clark Courts (CCF) - Pickleball Only", ("ccf-pickleball", "ccf")),
    Facility(35, ("racquetball",), "Gregory Gym (GRE) - Racquetball / Handball", ("gre-rb", "gre-racquetball", "gre-handball")),
    Facility(40, ("squash",), "Gregory Gym (GRE) - Squash", ("gre-squash",)),
    Facility(55, ("squash",), "Recreational Sports Center (RSC) - Squash", ("rsc-squash",)),
    Facility(50, ("racquetball",), "Recreational Sports Center (RSC) - Racquetball / Handball", ("rsc-rb", "rsc-racquetball", "rsc-handball")),
    Facility(60, ("tennis",), "Whitaker Courts (WC) - Tennis Only", ("wc-tennis",)),
    Facility(65, ("pickleball", "tennis"), "Whitaker Courts (WC) - Pickleball / Tennis", ("wc-pickleball", "wc-mixed", "wc")),
]
FAC_BY_ID = {f.id: f for f in FACILITIES}

SPORT_ALIASES = {
    "squash": "squash", "sq": "squash",
    "racquetball": "racquetball", "rb": "racquetball", "racquet": "racquetball",
    "handball": "racquetball", "hb": "racquetball",
    "pickleball": "pickleball", "pb": "pickleball", "pickle": "pickleball",
    "tennis": "tennis", "tn": "tennis",
}

STATUS_SYMBOL = {
    "open": "✓", "reserved": "·", "event": "E", "priority": "P",
    "closed": "×", "yours": "★", "unavailable": "-",
}
LEGEND = "✓ open  · reserved  E event  P faculty/staff priority  × closed  ★ yours  - other"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class UTCourtsError(Exception):
    exit_code = 1


class SignInNeeded(UTCourtsError):
    exit_code = 2


class PageChanged(UTCourtsError):
    exit_code = 3


class NetworkTrouble(UTCourtsError):
    exit_code = 3


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def austin_today() -> date:
    return datetime.now(AUSTIN_TZ).date() if AUSTIN_TZ else date.today()


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_date(text: str, today: date | None = None) -> date | None:
    """today / tomorrow / fri / +3 / 2026-11-20 / 11/20 / 11/20/2026 -> date (None if not a date)."""
    today = today or austin_today()
    t = text.strip().lower()
    if t in ("today", "tod", "now"):
        return today
    if t in ("tomorrow", "tmr", "tmrw", "tom"):
        return today + timedelta(days=1)
    if re.fullmatch(r"\+\d{1,2}", t):
        return today + timedelta(days=int(t[1:]))
    for i, wd in enumerate(WEEKDAYS):
        full = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][i]
        if t in (wd, full, wd[:2]) or (len(t) >= 3 and full.startswith(t)):
            return today + timedelta(days=(i - today.weekday()) % 7)
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", t)
        if m:
            year = int(m[3]) if m[3] else today.year
            if year < 100:
                year += 2000
            d = date(year, int(m[1]), int(m[2]))
            if not m[3] and d < today - timedelta(days=30):  # "1/3" in December means next year
                d = date(year + 1, d.month, d.day)
            return d
    except ValueError as e:
        raise UTCourtsError(f"{text!r} isn't a real date ({e})") from e
    return None


def parse_clock(text: str) -> int:
    """'17', '17:30', '5pm', '5:30 pm', '5p' -> minutes after midnight."""
    t = text.strip().lower().replace(" ", "").replace(".", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm|a|p)?", t)
    if not m:
        raise UTCourtsError(f"Couldn't read the time {text!r} (try 17, 17:30 or 5pm)")
    h, mins, ampm = int(m[1]), int(m[2] or 0), m[3]
    if ampm:
        if not 1 <= h <= 12:
            raise UTCourtsError(f"Couldn't read the time {text!r}")
        h = h % 12 + (12 if ampm.startswith("p") else 0)
    if not (0 <= h <= 24 and 0 <= mins < 60):
        raise UTCourtsError(f"Couldn't read the time {text!r}")
    return h * 60 + mins


def label_to_minutes(label: str) -> int:
    """'2:00 PM' -> 840"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?\s*", label)
    if not m:
        raise PageChanged(f"Unexpected time label on the page: {label!r}")
    h = int(m[1]) % 12 + (12 if m[3].lower() == "p" else 0)
    return h * 60 + int(m[2])


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def resolve_facilities(terms: Iterable[str]) -> list[Facility]:
    chosen: list[Facility] = []
    for raw in terms:
        t = raw.strip().lower()
        hits: list[Facility] = []
        if t in ("all", "any", "*"):
            hits = list(FACILITIES)
        elif t.isdigit() and int(t) in FAC_BY_ID:
            hits = [FAC_BY_ID[int(t)]]
        elif t in SPORT_ALIASES:
            hits = [f for f in FACILITIES if SPORT_ALIASES[t] in f.sports]
        else:
            hits = [f for f in FACILITIES if t in f.aliases]
        if not hits:
            raise UTCourtsError(f"Unknown sport/facility {raw!r}. Run `utcourts facilities` to see the options.")
        for f in hits:
            if f not in chosen:
                chosen.append(f)
    return chosen


# --------------------------------------------------------------------------- #
# Parsing the court page
# --------------------------------------------------------------------------- #
@dataclass
class Slot:
    time: str          # "14:00"
    label: str         # "2:00 PM"
    court: str         # "1", "A", "B1A", ...
    status: str        # open | reserved | event | priority | closed | yours | unavailable
    detail: str = ""   # e.g. "Event Conflict: Handball (UTSCA)"
    slot_id: str | None = None  # the site's id for this slot (only for open ones)
    href: str | None = field(default=None, repr=False)  # booking link — opening it books the court


@dataclass
class DayResult:
    date: str
    weekday: str
    facility_id: int
    facility: str
    courts: list[str]
    slots: list[Slot] = field(default_factory=list)

    def open_by_time(self, after: int | None = None, before: int | None = None) -> list[dict]:
        out: dict[str, dict] = {}
        for s in self.slots:
            mins = int(s.time[:2]) * 60 + int(s.time[3:])
            if after is not None and mins < after:
                continue
            if before is not None and mins >= before:
                continue
            if s.status == "open":
                out.setdefault(s.time, {"time": s.time, "label": s.label, "courts": []})["courts"].append(s.court)
        return list(out.values())


def _classify(detail: str) -> str:
    d = detail.lower()
    if not d or d == "reserved":
        return "reserved"
    if d.startswith("event conflict"):
        return "event"
    if "closed" in d:
        return "closed"
    if "priority" in d:
        return "priority"
    if "your" in d or "my reservation" in d:
        return "yours"
    return "unavailable"


def parse_courts_page(html: str) -> DayResult:
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(id="form_courts") is None:
        raise PageChanged("No court form on the page")

    # Which day did the server actually give us? (It silently clamps out-of-range dates.)
    date_input = soup.find("input", id="date")
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", (date_input.get("value") if date_input else "") or "")
    if not m:
        raise PageChanged("Couldn't find the selected date on the page")
    page_date = date(int(m[3]), int(m[1]), int(m[2]))

    # Which facility? The page selects it with an inline `$("#facility_id").val("35")`.
    fac_id = None
    m = re.search(r"#facility_id[\"']\)\.val\([\"'](\d+)[\"']\)", html)
    if m:
        fac_id = int(m[1])
    else:
        opt = soup.select_one("#facility_id option[selected]")
        if opt and str(opt.get("value", "")).isdigit():
            fac_id = int(opt["value"])
    names = {int(o["value"]): o.get_text(" ", strip=True)
             for o in soup.select("#facility_id option") if str(o.get("value", "")).isdigit()}
    fac_name = names.get(fac_id) or (FAC_BY_ID[fac_id].name if fac_id in FAC_BY_ID else f"facility {fac_id}")

    result = DayResult(date=page_date.isoformat(), weekday=page_date.strftime("%a"),
                       facility_id=fac_id or -1, facility=fac_name, courts=[])

    table = soup.find("table")
    if table is None:
        return result  # nothing bookable that day (or every hour already passed)

    # The raw markup has no <tbody> and leaves the header <tr> unclosed, so don't lean on
    # nesting: header = every <th>, rows = every <tr> that has its own <td>s.
    head_cells = table.find_all("th")
    result.courts = [th.get_text(" ", strip=True) for th in head_cells[1:]]

    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        label = tds[0].get_text(" ", strip=True)
        if not label:
            continue
        t = hhmm(label_to_minutes(label))
        for i, td in enumerate(tds[1:]):
            court = result.courts[i] if i < len(result.courts) else str(i + 1)
            a = td.find("a", href=True)
            if a is not None:
                q = parse_qs(urlparse(a["href"]).query)
                action = (q.get("reservationAction") or [""])[0].lower()
                if action == "reserve":
                    result.slots.append(Slot(t, label, court, "open", slot_id=(q.get("id") or [None])[0],
                                             href=urljoin(COURTS_URL, a["href"])))
                    continue
                if "cancel" in action:
                    result.slots.append(Slot(t, label, court, "yours", detail="Your reservation"))
                    continue
            raw = td.get("data-original-title") or td.get("title") or ""
            detail = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True) if "<" in raw else raw.strip()
            detail = re.sub(r"^Unavailable\s*-\s*Reason:\s*", "", detail)
            result.slots.append(Slot(t, label, court, _classify(detail), detail=detail or "Reserved"))
    return result


# --------------------------------------------------------------------------- #
# Session storage
# --------------------------------------------------------------------------- #
def load_session() -> dict | None:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_session(cookies: list[dict], user_agent: str | None,
                 browser_cookies: list[dict] | None = None) -> None:
    """cookies: what the fast path sends to RecSports.
    browser_cookies: the hidden browser's full jar (incl. UT SSO). Browsers throw away
    "session" cookies when they close, and SSO cookies are usually that kind, so we keep
    them here and put them back on the next launch — that's what makes quiet re-login work."""
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(HOME, 0o700)
    except OSError:
        pass
    if browser_cookies is None:
        browser_cookies = (load_session() or {}).get("browser_cookies", [])
    data = {"saved_at": datetime.now().isoformat(timespec="seconds"),
            "user_agent": user_agent or DEFAULT_UA, "cookies": cookies,
            "browser_cookies": browser_cookies}
    tmp = SESSION_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, SESSION_FILE)


def _cookie_applies(c: dict, host: str) -> bool:
    dom = (c.get("domain") or "").lower()
    if not dom:
        return False
    if dom.startswith("."):
        return host == dom[1:] or host.endswith(dom)
    return host == dom


# --------------------------------------------------------------------------- #
# Talking to the site. Two interchangeable clients:
#   HttpClient    — plain HTTP with the saved cookies (fast, no browser)
#   BrowserClient — requests made from inside the hidden browser (after an SSO re-login)
# Commands are written as `action(client)` and run through `with_client`, which tries
# the fast path first and falls back to re-login + browser automatically.
# --------------------------------------------------------------------------- #
@dataclass
class Page:
    url: str
    status: int
    text: str


def _looks_signed_in(html: str) -> bool:
    return ('id="form_courts"' in html or "id='form_courts'" in html
            or re.search(r'href=["\'][^"\']*/logout["\']', html) is not None)


def _check_page(url: str, status: int, text: str) -> Page:
    p = urlparse(url)
    if p.hostname != APP_HOST or not p.path.startswith(APP_PREFIX):
        raise SignInNeeded("RecSports sent us to a sign-in page.")
    if status in (401, 403):
        raise SignInNeeded("RecSports session expired.")
    if status >= 400:
        raise NetworkTrouble(f"{APP_HOST} answered HTTP {status}")
    if not _looks_signed_in(text):
        raise SignInNeeded("RecSports showed a page that doesn't look signed in.")
    return Page(url, status, text)


class HttpClient:
    parallel = True

    def __init__(self, session: dict):
        self.session = session
        now = time.time()
        self.client = httpx.Client(
            headers={"User-Agent": session.get("user_agent") or DEFAULT_UA,
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9",
                     "Referer": COURTS_URL},
            follow_redirects=False, timeout=20.0)
        for c in session.get("cookies") or []:
            exp = c.get("expires", -1)
            if exp not in (-1, None) and exp > 0 and exp < now:
                continue
            if _cookie_applies(c, APP_HOST):
                self.client.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path") or "/")

    def _send(self, method: str, url: str, params=None, data=None) -> Page:
        try:
            r = self.client.request(method, url, params=params, data=data)
            for _ in range(5):  # follow redirects only while they stay inside My RecSports
                if not r.is_redirect:
                    break
                nxt = urljoin(str(r.url), r.headers.get("location", ""))
                p = urlparse(nxt)
                if p.hostname != APP_HOST or not p.path.startswith(APP_PREFIX):
                    raise SignInNeeded("RecSports session expired.")
                r = self.client.get(nxt)
        except httpx.HTTPError as e:
            raise NetworkTrouble(f"Couldn't reach {APP_HOST}: {e}") from e
        return _check_page(str(r.url), r.status_code, r.text)

    def get(self, url: str, params: dict | None = None) -> Page:
        return self._send("GET", url, params=params)

    def post(self, url: str, data: dict) -> Page:
        return self._send("POST", url, data=data)

    def close(self) -> None:
        """Save any cookie the app refreshed, then close."""
        stored = self.session.get("cookies") or []
        index = {(c["name"], c.get("domain"), c.get("path") or "/"): c for c in stored}
        changed = False
        for ck in self.client.cookies.jar:
            key = (ck.name, ck.domain, ck.path or "/")
            if key in index:
                if index[key]["value"] != ck.value:
                    index[key]["value"] = ck.value
                    if ck.expires:
                        index[key]["expires"] = ck.expires
                    changed = True
            else:
                stored.append({"name": ck.name, "value": ck.value, "domain": ck.domain,
                               "path": ck.path or "/", "expires": ck.expires or -1,
                               "httpOnly": True, "secure": bool(ck.secure), "sameSite": "Lax"})
                changed = True
        if changed:
            save_session(stored, self.session.get("user_agent"), self.session.get("browser_cookies"))
        self.client.close()


class BrowserClient:
    parallel = False  # Playwright's sync API isn't thread-safe

    def __init__(self, ctx):
        self.ctx = ctx

    def get(self, url: str, params: dict | None = None) -> Page:
        r = self.ctx.request.get(url, params=params, fail_on_status_code=False, timeout=30_000)
        return _check_page(r.url, r.status, r.text())

    def post(self, url: str, data: dict) -> Page:
        r = self.ctx.request.post(url, form=data, fail_on_status_code=False, timeout=30_000)
        return _check_page(r.url, r.status, r.text())


def fetch_grid_page(client, day: date, facility_id: int) -> tuple[DayResult, Page]:
    pg = client.get(COURTS_URL, params={"date": day.strftime("%m/%d/%Y"), "facility_id": str(facility_id)})
    if 'id="form_courts"' not in pg.text and "id='form_courts'" not in pg.text:
        raise PageChanged("The court page didn't have the court form on it.")
    return parse_courts_page(pg.text), pg


def fetch_grids(client, jobs: list[tuple[date, int]]) -> list[DayResult]:
    first = fetch_grid_page(client, *jobs[0])[0]  # a dead session fails on one request, not eight
    if len(jobs) == 1:
        return [first]
    if client.parallel:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rest = list(pool.map(lambda j: fetch_grid_page(client, *j)[0], jobs[1:]))
    else:
        rest = [fetch_grid_page(client, *j)[0] for j in jobs[1:]]
    return [first, *rest]


def fetch_many(jobs: list[tuple[date, int]]) -> list[DayResult]:
    """Fast path only (raises SignInNeeded if the saved cookies don't work)."""
    sess = load_session()
    if not sess or not sess.get("cookies"):
        raise SignInNeeded("No saved session yet.")
    client = HttpClient(sess)
    try:
        return fetch_grids(client, jobs)
    finally:
        client.close()


def with_client(action, allow_login: bool):
    """Run action(client): fast path first; on an expired session, quiet SSO re-login in the
    hidden browser and run it there; if UT wants a human, open a window (when allowed)."""
    sess = load_session()
    if sess and sess.get("cookies"):
        client = HttpClient(sess)
        try:
            return action(client)
        except SignInNeeded:
            pass
        finally:
            client.close()
        log("RecSports session expired — trying to sign back in quietly…")
        ok, value = browser_session(interactive=False, timeout_s=30, action=action)
        if ok:
            log("Signed back in.")
            return value
    elif PROFILE_DIR.exists():
        ok, value = browser_session(interactive=False, timeout_s=30, action=action)
        if ok:
            return value
    elif not allow_login:
        raise SignInNeeded("Not signed in yet. Run `utcourts login` once.")
    if allow_login:
        return interactive_login(action)
    raise SignInNeeded("UT sign-in has expired. Run `utcourts login` (needs your EID + Duo).")


def get_results(jobs: list[tuple[date, int]], allow_login: bool) -> list[DayResult]:
    return with_client(lambda c: fetch_grids(c, jobs), allow_login)


# --------------------------------------------------------------------------- #
# Slow path: a real browser with a persistent profile (Playwright)
# --------------------------------------------------------------------------- #
def _launch(pw, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(HOME, 0o700)
    except OSError:
        pass
    attempts = [{"channel": "chrome"}, {"channel": "msedge"}, {}]
    if os.environ.get("UTCOURTS_BROWSER") == "chromium":
        attempts = [{}]
    last: Exception | None = None
    for extra in attempts:
        try:
            return pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=headless,
                no_viewport=not headless, **extra)
        except Exception as e:  # browser missing / profile locked / ...
            last = e
            if "ProcessSingleton" in str(e) or "SingletonLock" in str(e) or "already in use" in str(e):
                raise UTCourtsError("The utcourts browser profile is already open (another `utcourts login` running?).") from e
    raise UTCourtsError(
        "Couldn't start a browser for sign-in. Install Google Chrome, or run:\n"
        "    uvx playwright install chromium\n"
        f"(last error: {str(last).splitlines()[0] if last else 'unknown'})")


_PW_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def restore_browser_cookies(ctx) -> None:
    saved = (load_session() or {}).get("browser_cookies") or []
    now = time.time()
    live = [{k: c[k] for k in _PW_COOKIE_KEYS if k in c} for c in saved
            if c.get("expires", -1) in (-1, None) or c.get("expires", -1) <= 0 or c["expires"] > now]
    for c in live:
        if c.get("expires") is None:
            c["expires"] = -1
    try:
        if live:
            ctx.add_cookies(live)
    except Exception:
        for c in live:  # one bad cookie shouldn't sink the rest
            try:
                ctx.add_cookies([c])
            except Exception:
                pass


def remember_browser(ctx, user_agent: str | None) -> None:
    """Save the app cookies (fast path) and the whole jar (quiet re-login next time)."""
    jar = ctx.cookies()
    save_session([c for c in jar if _cookie_applies(c, APP_HOST)], user_agent, jar)


def _needs_user(page) -> bool:
    """True if the page is waiting for a human (password box or Duo prompt)."""
    try:
        if any("duosecurity.com" in (fr.url or "") or "duo" in (urlparse(fr.url or "").hostname or "")
               for fr in page.frames):
            return True
        pw_box = page.query_selector("input[type=password]")
        return bool(pw_box and pw_box.is_visible())
    except Exception:
        return False


def browser_session(interactive: bool, timeout_s: float, action=None) -> tuple[bool, object]:
    """Open the private profile and get to the court page (signing in via SSO if needed).

    On success: saves the cookies for the fast path, runs `action(BrowserClient)` from inside
    the browser (so it works even if exported cookies ever stop working), and returns
    (True, its result). Returns (False, None) if a human is needed.
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except ImportError as e:
        raise UTCourtsError("Playwright isn't installed. Run via `uv run utcourts.py ...` "
                            "or `pip install playwright`.") from e

    with sync_playwright() as pw:
        ctx = _launch(pw, headless=not interactive)
        try:
            restore_browser_cookies(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ua = None
            try:
                ua = page.evaluate("navigator.userAgent")
            except PWError:
                pass
            if ua and "Headless" in ua:
                ua = ua.replace("HeadlessChrome", "Chrome")
                ctx.set_extra_http_headers({"User-Agent": ua})
            try:
                page.goto(COURTS_URL, wait_until="domcontentloaded", timeout=30_000)
            except PWError as e:
                if "net::" in str(e) and not interactive:
                    raise NetworkTrouble(f"Couldn't reach {APP_HOST}: {str(e).splitlines()[0]}") from e

            deadline = time.monotonic() + timeout_s
            human_since: float | None = None
            last_url, url_since = "", time.monotonic()
            nudged = False
            while time.monotonic() < deadline:
                if page.is_closed():
                    if interactive:
                        raise SignInNeeded("The sign-in window was closed before sign-in finished.")
                    return False, None
                try:
                    url = page.url
                    host = urlparse(url).hostname
                    if url != last_url:
                        last_url, url_since = url, time.monotonic()
                    if host == APP_HOST and page.query_selector("#form_courts"):
                        remember_browser(ctx, ua or page.evaluate("navigator.userAgent"))
                        value = action(BrowserClient(ctx)) if action else None
                        remember_browser(ctx, ua or page.evaluate("navigator.userAgent"))
                        return True, value
                    if host == APP_HOST and not nudged and urlparse(url).path != COURTS_PATH:
                        # signed in but landed on the app's home page — go to courts
                        page.wait_for_timeout(1500)
                        if urlparse(page.url).hostname == APP_HOST and not page.query_selector("#form_courts"):
                            nudged = True
                            page.goto(COURTS_URL, wait_until="domcontentloaded", timeout=30_000)
                            continue
                    if not interactive:
                        if _needs_user(page):
                            human_since = human_since or time.monotonic()
                            if time.monotonic() - human_since > 2.5:
                                return False, None  # UT wants a password / Duo: needs a human
                        else:
                            human_since = None
                        # SSO redirect chains don't sit still; a login page waiting for a human does.
                        if host != APP_HOST and time.monotonic() - url_since > 8:
                            return False, None
                except PWError:
                    pass  # mid-navigation; try again
                page.wait_for_timeout(400)
            return False, None
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def interactive_login(action=None):
    log("Opening a browser window — sign in with your UT EID (and Duo).")
    log("Tip: if Duo offers \"trust this browser\" / \"remember me\", say yes so this lasts longer.")
    ok, value = browser_session(interactive=True, timeout_s=300, action=action)
    if not ok:
        raise SignInNeeded("Didn't see the court page within 5 minutes. Run `utcourts login` to try again.")
    log(f"Signed in. Session saved to {SESSION_FILE}")
    return value


# --------------------------------------------------------------------------- #
# Messages the site shows after an action ("Reservation confirmed", "limit reached", ...)
# --------------------------------------------------------------------------- #
_ERRORISH = re.compile(r"\b(error|unable|cannot|can't|not allowed|maximum|limit|exceed|already|"
                       r"denied|invalid|fail|too (late|early)|restricted|not available|sorry)\b", re.I)
_OKISH = re.compile(r"\b(success|confirmed|reserved|booked|cancel+ed|removed|thank)\b", re.I)


def page_messages(html: str) -> list[dict]:
    """Alert boxes and JS alert()/swal()/toastr popups on a page, as [{kind, text}]."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, text: str) -> None:
        text = re.sub(r"\s+", " ", text).strip(" ××")
        if 3 <= len(text) <= 400 and text not in seen:
            seen.add(text)
            if kind == "info":
                kind = "error" if _ERRORISH.search(text) else "ok" if _OKISH.search(text) else "info"
            out.append({"kind": kind, "text": text})

    for el in soup.select(".alert, .text-danger, .invalid-feedback, .toast-body"):
        cls = " ".join(el.get("class", []))
        kind = ("error" if re.search(r"danger|warning|invalid", cls) else
                "ok" if "success" in cls else "info")
        add(kind, el.get_text(" ", strip=True))
    for sc in soup.find_all("script"):
        for m in re.finditer(r"(?:\balert|\bswal|Swal\.fire|toastr\.(\w+))\(\s*(['\"])(.{3,400}?)\2",
                             sc.get_text() or ""):
            kind = {"error": "error", "warning": "error", "success": "ok"}.get((m[1] or "").lower(), "info")
            add(kind, m[3])
    return out


def new_messages(after: str, before: str | None) -> list[dict]:
    base = {m["text"] for m in page_messages(before)} if before else set()
    return [m for m in page_messages(after) if m["text"] not in base]


# --------------------------------------------------------------------------- #
# My Reservations
# --------------------------------------------------------------------------- #
@dataclass
class Reservation:
    index: int
    text: str
    cancel_method: str | None = None    # "GET" | "POST" | None (cancel on the website)
    cancel_url: str | None = field(default=None, repr=False)
    cancel_data: dict | None = field(default=None, repr=False)


def _is_cancel_link(a) -> bool:
    blob = " ".join([a.get("href", ""), a.get_text(" "), a.get("title", ""),
                     a.get("aria-label", ""), a.get("onclick", "")]).lower()
    return "cancel" in blob


def _is_cancel_form(form) -> bool:
    bits = [form.get("action") or "", form.get("id") or "", form.get("name") or ""]
    for b in form.find_all(["button", "input"]):
        if b.name == "button" or (b.get("type") or "").lower() in ("submit", "button", "hidden"):
            bits += [b.get_text(" "), b.get("value") or "", b.get("name") or ""]
    return "cancel" in " ".join(bits).lower()


def parse_reservations(html: str, page_url: str = MINE_URL) -> tuple[list[Reservation], list[str]]:
    """Find reservations by their cancel buttons (links or forms) on My Reservations.

    The exact markup for court reservations hasn't been seen yet, so this deliberately
    doesn't depend on table layout: each cancel control's reservation is the largest
    enclosing element that contains no other cancel control."""
    soup = BeautifulSoup(html, "html.parser")
    controls = [("GET", a) for a in soup.find_all("a", href=True) if _is_cancel_link(a)]
    controls += [("POST", f) for f in soup.find_all("form") if _is_cancel_form(f)]
    control_ids = {id(el) for _, el in controls}

    def n_controls(el) -> int:
        n = 1 if id(el) in control_ids else 0
        return n + sum(1 for d in el.descendants if id(d) in control_ids)

    def is_section(el) -> bool:  # widening into this would swallow headings / other content
        return (el.name in ("body", "html", "[document]", "table")
                or el.find(["h1", "h2", "h3", "h4", "h5", "h6", "table"]) is not None
                or el.select_one(".card-header, .modal") is not None)

    items: list[Reservation] = []
    for method, el in controls:
        box = el.find_parent("tr") or el.find_parent("li")
        if box is None or n_controls(box) != 1:
            box = el
            while (box.parent is not None and box.parent.name not in ("body", "html", "[document]")
                   and n_controls(box.parent) == 1 and not is_section(box.parent)):
                box = box.parent
        # a GET link inside a cancel form: the form handles it
        if method == "GET" and el.find_parent("form") is not None and id(el.find_parent("form")) in control_ids:
            continue
        text = box.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", re.sub(r"\bCancel( Reservation)?\b", " ", text, flags=re.I)).strip(" -|·")
        if method == "GET":
            href = el["href"].strip()
            if href.lower().startswith(("javascript:", "#")) or not href:
                items.append(Reservation(0, text))  # handled by page JavaScript: can't do it from here
                continue
            items.append(Reservation(0, text, "GET", urljoin(page_url, href)))
        else:
            data = {}
            for inp in el.find_all(["input", "select", "textarea"]):
                name = inp.get("name")
                if not name or (inp.get("type") or "").lower() in ("submit", "button", "image", "reset"):
                    continue
                if inp.name == "select":
                    opt = inp.find("option", selected=True) or inp.find("option")
                    data[name] = opt.get("value", opt.get_text()) if opt else ""
                else:
                    data[name] = inp.get("value", "")
            btn = next((b for b in el.find_all(["button", "input"])
                        if b.get("name") and "cancel" in (b.get_text(" ") + (b.get("value") or "")).lower()), None)
            if btn is not None:
                data[btn["name"]] = btn.get("value", "")
            if (el.get("method") or "get").lower() == "get":
                items.append(Reservation(0, text, "GET-FORM", urljoin(page_url, el.get("action") or page_url), data))
            else:
                items.append(Reservation(0, text, "POST", urljoin(page_url, el.get("action") or page_url), data))
    for i, it in enumerate(items, 1):
        it.index = i

    notices = []
    for el in soup.find_all(["p", "div", "li", "td", "span", "h5", "h6"]):
        own = el.get_text(" ", strip=True)
        if len(own) < 200 and re.search(r"\bno (upcoming|current|future)?\s*\w*\s*reservations?\b", own, re.I):
            own = re.sub(r"\s+", " ", own)
            if any(n in own for n in notices):
                continue  # a shorter notice already covers this one
            notices = [n for n in notices if own not in n] + [own]
    return items, notices


def fetch_reservations(client) -> tuple[list[Reservation], list[str], Page]:
    pg = client.get(MINE_URL)
    items, notices = parse_reservations(pg.text, pg.url)
    return items, notices, pg


def send_cancel(client, r: Reservation) -> Page:
    if r.cancel_method == "GET":
        return client.get(r.cancel_url)
    if r.cancel_method == "GET-FORM":
        return client.get(r.cancel_url, params=r.cancel_data)
    if r.cancel_method == "POST":
        return client.post(r.cancel_url, r.cancel_data or {})
    raise UTCourtsError("This reservation's cancel button runs JavaScript on the page; cancel it on the website.")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _fmt_header(r: DayResult) -> str:
    d = date.fromisoformat(r.date)
    return f"{r.facility} — {d.strftime('%a %b')} {d.day}"


def print_open(results: list[DayResult], after: int | None, before: int | None) -> None:
    color = _use_color()
    g, b, dim, reset = ("\033[32m", "\033[1m", "\033[2m", "\033[0m") if color else ("", "", "", "")
    any_open = False
    for r in results:
        rows = r.open_by_time(after, before)
        print(f"{b}{_fmt_header(r)}{reset}")
        if not r.courts:
            print(f"  {dim}nothing bookable left on this day{reset}")
        elif not rows:
            print(f"  {dim}no open courts{' in that window' if (after or before) else ''}{reset}")
        else:
            any_open = True
            w = max(len(x["label"]) for x in rows)
            for x in rows:
                courts = ", ".join(x["courts"])
                n = len(x["courts"])
                print(f"  {x['label']:>{w}}  {g}{courts}{reset}  {dim}({n} open){reset}")
        print()
    if not any_open:
        print("No open courts found.")


def print_grid(results: list[DayResult], after: int | None, before: int | None) -> None:
    color = _use_color()
    colors = {"open": "\033[32m", "yours": "\033[36m", "event": "\033[33m", "priority": "\033[35m"} if color else {}
    reset = "\033[0m" if color else ""
    for r in results:
        print(_fmt_header(r))
        if not r.courts:
            print("  nothing bookable left on this day\n")
            continue
        cw = max(2, *(len(c) for c in r.courts)) + 1
        print("  " + " " * 8 + "".join(c.rjust(cw) for c in r.courts))
        by_time: dict[str, dict[str, Slot]] = {}
        for s in r.slots:
            by_time.setdefault(s.time, {})[s.court] = s
        for t, cells in by_time.items():
            mins = int(t[:2]) * 60 + int(t[3:])
            if (after is not None and mins < after) or (before is not None and mins >= before):
                continue
            label = next(iter(cells.values())).label
            line = f"  {label:>8}"
            for c in r.courts:
                s = cells.get(c)
                sym = STATUS_SYMBOL.get(s.status, "?") if s else " "
                col = colors.get(s.status, "") if s else ""
                line += " " * (cw - 1) + f"{col}{sym}{reset if col else ''}"
            print(line)
        events = sorted({s.detail for s in r.slots if s.status in ("event", "unavailable", "priority")})
        if events:
            print("  " + "; ".join(events))
        print()
    print(LEGEND)


def as_json(results: list[DayResult], after: int | None, before: int | None, include_all: bool) -> str:
    out = []
    for r in results:
        item = {"date": r.date, "weekday": r.weekday, "facility_id": r.facility_id,
                "facility": r.facility, "courts": r.courts,
                "open": r.open_by_time(after, before)}
        if include_all:
            item["slots"] = [{k: v for k, v in asdict(s).items() if k != "href"} for s in r.slots
                             if (after is None or int(s.time[:2]) * 60 + int(s.time[3:]) >= after)
                             and (before is None or int(s.time[:2]) * 60 + int(s.time[3:]) < before)]
        out.append(item)
    return json.dumps({"generated_at": datetime.now(AUSTIN_TZ).isoformat(timespec="seconds"),
                       "source": COURTS_URL, "results": out}, indent=2)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_check(args) -> int:
    today = austin_today()
    sport_terms: list[str] = list(args.sport or [])
    day: date | None = parse_date(args.date) if args.date else None
    if args.date and day is None:
        raise UTCourtsError(f"Couldn't read the date {args.date!r}")
    for term in args.terms:
        d = parse_date(term, today)
        if d is not None and term.lower() not in SPORT_ALIASES:
            if day is not None:
                raise UTCourtsError("Give just one date (use --days N for a range).")
            day = d
        else:
            sport_terms.append(term)
    day = day or today
    facilities = resolve_facilities(sport_terms) if sport_terms else list(FACILITIES)

    last_ok = today + timedelta(days=MAX_DAYS_AHEAD)
    if day < today:
        raise UTCourtsError(f"{day:%a %b %d} is in the past.")
    if day > last_ok:
        raise UTCourtsError(f"RecSports only shows up to {MAX_DAYS_AHEAD} days ahead (last day: {last_ok:%a %b %d}).")
    days = [day + timedelta(days=i) for i in range(max(1, args.days)) if day + timedelta(days=i) <= last_ok]
    if args.days > len(days):
        log(f"(Only showing through {last_ok:%a %b %d} — that's as far ahead as RecSports goes.)")

    after = parse_clock(args.after) if args.after else None
    before = parse_clock(args.before) if args.before else None
    jobs = [(d, f.id) for d in days for f in facilities]
    results = get_results(jobs, allow_login=args.login and sys.stdin.isatty())

    # Sanity check: the site clamps dates it won't show instead of erroring.
    for (d, fid), r in zip(jobs, results):
        if r.date != d.isoformat():
            log(f"Warning: asked for {d.isoformat()} but RecSports returned {r.date}.")
        if r.facility_id not in (fid, -1):
            log(f"Warning: asked for facility {fid} but RecSports returned {r.facility_id}.")

    if args.json:
        print(as_json(results, after, before, args.all))
    elif args.grid or args.all:
        print_grid(results, after, before)
    else:
        print_open(results, after, before)
    return 0


def cmd_login(args) -> int:
    interactive_login()
    # Make sure the quick (no-browser) path works with the saved cookies.
    try:
        r = fetch_many([(austin_today(), 35)])[0]
        log(f"All set — quick checks work ({r.facility}, {r.date}).")
    except SignInNeeded:
        log("Signed in, but RecSports rejected the saved cookies outside the browser. "
            "Checks will still work; they'll just go through the hidden browser (a bit slower).")
    return 0


def cmd_status(args) -> int:
    sess = load_session()
    if not sess:
        print("Not signed in. Run `utcourts login`.")
        return 2
    try:
        fetch_many([(austin_today(), 35)])
        if not args.quiet:
            print(f"Signed in (session saved {sess.get('saved_at', '?')}).")
        return 0
    except SignInNeeded:
        if args.refresh and browser_session(interactive=False, timeout_s=30)[0]:
            if not args.quiet:
                print("Session had expired; signed back in quietly.")
            return 0
        print("Session expired. Run `utcourts login`." if not args.refresh
              else "Session expired and UT SSO wants a fresh sign-in. Run `utcourts login`.")
        return 2


def cmd_logout(args) -> int:
    removed = False
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        removed = True
    if PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        removed = True
    print("Removed the saved session and browser profile." if removed else "Nothing to remove.")
    return 0


def cmd_facilities(args) -> int:
    if args.json:
        print(json.dumps([{"id": f.id, "name": f.name, "sports": list(f.sports), "aliases": list(f.aliases)}
                          for f in FACILITIES], indent=2))
        return 0
    print(f"{'id':>3}  {'short names':<40} facility")
    for f in FACILITIES:
        print(f"{f.id:>3}  {', '.join(f.aliases):<40} {f.name}")
    print("\nSports: squash (sq) · racquetball (rb, handball) · pickleball (pb) · tennis")
    return 0


# --------------------------------------------------------------------------- #
# book / mine / cancel
# --------------------------------------------------------------------------- #
NOTHING_DONE = 4  # exit code: slot not open, not confirmed, ambiguous, or RecSports said no


def _emit(args, payload: dict, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(text)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _is_facility_word(t: str) -> bool:
    t = t.lower()
    return (t in SPORT_ALIASES or t in ("all", "any") or any(t in f.aliases for f in FACILITIES)
            or (t.isdigit() and int(t) in FAC_BY_ID))


def _split_book_terms(terms: list[str], today: date) -> tuple[list[str], date | None, int | None]:
    facs, day, at = [], None, None
    for t in terms:
        if _is_facility_word(t):
            facs.append(t)
            continue
        d = parse_date(t, today)
        if d is not None:
            if day is not None:
                raise UTCourtsError("Give just one date.")
            day = d
            continue
        try:
            m = parse_clock(t)
        except UTCourtsError:
            raise UTCourtsError(f"Didn't understand {t!r}. Try: utcourts book gre-squash tomorrow 9pm --court B")
        if at is not None:
            raise UTCourtsError("Give just one time.")
        at = m
    return facs, day, at


def _describe(facility: str, day: str, label: str, court: str) -> str:
    d = date.fromisoformat(day)
    return f"{facility}, court {court}, {d:%a %b} {d.day} at {label}"


def _find_slot(g: DayResult, t: str, court: str) -> Slot | None:
    return next((s for s in g.slots if s.time == t and s.court.lower() == court.lower()), None)


def _print_messages(msgs: list[dict]) -> str:
    return "".join(f"\n  RecSports said: {m['text']}" for m in msgs)


def make_book_action(day: date, facility_id: int, t: str, court: str):
    state = {"sent": False}

    def action(client) -> dict:
        g, before = fetch_grid_page(client, day, facility_id)
        s = _find_slot(g, t, court)
        if s is None or s.status != "open":
            if state["sent"]:
                return {"status": "unclear", "messages": [],
                        "note": "It's not open anymore. The first try may have gone through; check `utcourts mine`."}
            return {"status": "taken", "messages": [],
                    "note": f"Not open anymore ({s.detail if s else 'not on the grid'})."}
        if not s.href:
            raise PageChanged("Couldn't find the booking link for that slot.")
        state["sent"] = True
        resp = client.get(s.href)
        msgs = new_messages(resp.text, before.text)
        g2, _ = fetch_grid_page(client, day, facility_id)
        s2 = _find_slot(g2, t, court)
        gone = s2 is None or s2.status != "open"
        errors = [m for m in msgs if m["kind"] == "error"]
        status = "booked" if gone and not errors else "failed" if not gone else "unclear"
        return {"status": status, "messages": msgs, "now_shows": (s2.detail or s2.status) if s2 else None}

    return action


def cmd_book(args) -> int:
    today = austin_today()
    fac_terms, day, at = _split_book_terms(args.terms, today)
    if args.date:
        day = parse_date(args.date, today) or day
    if args.at:
        at = parse_clock(args.at)
    if not fac_terms:
        raise UTCourtsError("Say which sport or facility, e.g. `utcourts book gre-squash tomorrow 9pm`.")
    if at is None:
        raise UTCourtsError("Say what time, e.g. `9pm` or `21:00`.")
    day = day or today
    if day < today or day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise UTCourtsError(f"RecSports only books today through {today + timedelta(days=MAX_DAYS_AHEAD):%a %b %d}.")
    facilities = resolve_facilities(fac_terms)
    interactive = sys.stdin.isatty()
    allow_login = args.login and interactive
    want = hhmm(at)

    grids = get_results([(day, f.id) for f in facilities], allow_login)
    court_ok = (lambda s: s.court.lower() == args.court.lower()) if args.court else (lambda s: True)
    cands = [(g, s) for g in grids for s in g.slots if s.time == want and s.status == "open" and court_ok(s)]
    base = {"date": day.isoformat(), "time": want}

    if not cands:
        lines, near_all = [], []
        for g in grids:
            here = [s for s in g.slots if s.time == want and court_ok(s)]
            what = ", ".join(f"{s.court}: {s.detail or s.status}" for s in here) or "no slot at that time"
            opens = sorted(g.open_by_time(), key=lambda x: abs(int(x["time"][:2]) * 60 + int(x["time"][3:]) - at))[:3]
            near = "; ".join(f"{x['label']} ({', '.join(x['courts'])})" for x in sorted(opens, key=lambda x: x["time"]))
            near_all.append({"facility": g.facility, "at_that_time": what, "closest_open": opens})
            lines.append(f"  {g.facility}: {what}" + (f"\n    closest open: {near}" if near else ""))
        _emit(args, {**base, "status": "not_open", "facilities": near_all},
              f"Nothing open at {want} on {day:%a %b} {day.day}.\n" + "\n".join(lines))
        return NOTHING_DONE

    if len(cands) > 1:
        options = [{"facility": g.facility, "facility_id": g.facility_id, "court": s.court, "label": s.label}
                   for g, s in cands]
        if interactive and not args.json and not args.yes and not args.dry_run:
            for i, (g, s) in enumerate(cands, 1):
                print(f"  {i}. {_describe(g.facility, g.date, s.label, s.court)}")
            pick = _ask(f"Which one? [1-{len(cands)}, Enter to stop] ")
            if not pick.isdigit() or not 1 <= int(pick) <= len(cands):
                print("Not booked.")
                return NOTHING_DONE
            cands = [cands[int(pick) - 1]]
        else:
            _emit(args, {**base, "status": "ambiguous", "options": options},
                  "More than one court is open then — add --court and/or a facility short name:\n"
                  + "\n".join(f"  {_describe(g.facility, g.date, s.label, s.court)}" for g, s in cands))
            return NOTHING_DONE

    g, s = cands[0]
    what = _describe(g.facility, g.date, s.label, s.court)
    slot = {**base, "facility": g.facility, "facility_id": g.facility_id, "court": s.court, "label": s.label}
    if args.dry_run:
        _emit(args, {**slot, "status": "dry_run"}, f"Would book: {what}. (Dry run — nothing booked.)")
        return 0
    if not args.yes:
        if not interactive:
            _emit(args, {**slot, "status": "not_confirmed"},
                  f"Not booked: {what}. Add --yes to book without a prompt "
                  "(only once the person has said yes).")
            return NOTHING_DONE
        if _ask(f"Book {what}? [y/N] ").lower() not in ("y", "yes"):
            print("Not booked.")
            return NOTHING_DONE

    out = with_client(make_book_action(day, g.facility_id, s.time, s.court), allow_login)
    payload = {**slot, **out}
    msgs = _print_messages(out.get("messages", []))
    text = {
        "booked": f"Booked: {what}.{msgs}\n  (Double-checked: it no longer shows as open. Cancel with `utcourts mine` / `utcourts cancel`.)",
        "failed": f"Not booked: {what}.{msgs or chr(10) + '  The court still shows as open.'}",
        "unclear": f"Not sure it worked: {what}.{msgs}\n  {out.get('note') or 'Check `utcourts mine`.'}",
        "taken": f"Not booked — someone got there first: {what}. {out.get('note', '')}",
    }[out["status"]]
    _emit(args, payload, text)
    return 0 if out["status"] == "booked" else NOTHING_DONE


def cmd_mine(args) -> int:
    items, notices, _ = with_client(fetch_reservations, args.login and sys.stdin.isatty())
    payload = {"source": MINE_URL, "notices": notices,
               "reservations": [{"index": r.index, "text": r.text, "can_cancel": r.cancel_method is not None}
                                for r in items]}
    if items:
        lines = ["Your reservations:"]
        for r in items:
            lines.append(f"  {r.index}. {r.text}" + ("" if r.cancel_method else "   (cancel this one on the website)"))
        lines.append("\nCancel one with: utcourts cancel <number>")
        text = "\n".join(lines)
    else:
        text = "No reservations with a cancel option on My Reservations."
    if notices:
        text += "\n" + "\n".join(f"  {n}" for n in notices)
    _emit(args, payload, text)
    return 0


def _pick_reservation(items: list[Reservation], which: str | None, match: str | None) -> Reservation | None:
    if which:
        if not which.isdigit() or not 1 <= int(which) <= len(items):
            raise UTCourtsError(f"There's no reservation #{which}. Run `utcourts mine` to see the numbers.")
        return items[int(which) - 1]
    if match:
        words = match.lower().split()
        hits = [r for r in items if all(w in r.text.lower() for w in words)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise UTCourtsError(f"No reservation matches {match!r}. Run `utcourts mine`.")
        raise UTCourtsError(f"{len(hits)} reservations match {match!r}; be more specific or use the number.")
    return None


def make_cancel_action(text: str):
    state = {"sent": False}

    def action(client) -> dict:
        items, _, before = fetch_reservations(client)
        target = next((r for r in items if r.text == text), None)
        if target is None:
            return {"status": "unclear" if state["sent"] else "not_found", "messages": []}
        state["sent"] = True
        resp = send_cancel(client, target)
        msgs = new_messages(resp.text, before.text)
        items2, _, _ = fetch_reservations(client)
        still = any(r.text == text for r in items2)
        errors = [m for m in msgs if m["kind"] == "error"]
        return {"status": "failed" if still else ("unclear" if errors else "cancelled"), "messages": msgs}

    return action


def cmd_cancel(args) -> int:
    interactive = sys.stdin.isatty()
    allow_login = args.login and interactive
    items, notices, _ = with_client(fetch_reservations, allow_login)
    if not items:
        _emit(args, {"status": "not_found", "notices": notices}, "You have no reservations to cancel.")
        return NOTHING_DONE
    target = _pick_reservation(items, args.which, args.match)
    if target is None:
        listing = "\n".join(f"  {r.index}. {r.text}" for r in items)
        if interactive and not args.json and not args.yes:
            print(listing)
            pick = _ask(f"Which one? [1-{len(items)}, Enter to stop] ")
            if not pick:
                print("Nothing cancelled.")
                return NOTHING_DONE
            target = _pick_reservation(items, pick, None)
        else:
            _emit(args, {"status": "ambiguous", "reservations": [{"index": r.index, "text": r.text} for r in items]},
                  "Say which one: utcourts cancel <number>\n" + listing)
            return NOTHING_DONE
    base = {"reservation": target.text, "index": target.index}
    if target.cancel_method is None:
        _emit(args, {**base, "status": "unsupported"},
              f"Can't cancel this one from here (its button runs page JavaScript). Cancel it at {MINE_URL}")
        return NOTHING_DONE
    if args.dry_run:
        _emit(args, {**base, "status": "dry_run"}, f"Would cancel: {target.text}. (Dry run — nothing cancelled.)")
        return 0
    if not args.yes:
        if not interactive:
            _emit(args, {**base, "status": "not_confirmed"},
                  f"Not cancelled: {target.text}. Add --yes to cancel without a prompt "
                  "(only once the person has said yes).")
            return NOTHING_DONE
        if _ask(f"Cancel {target.text}? [y/N] ").lower() not in ("y", "yes"):
            print("Nothing cancelled.")
            return NOTHING_DONE
    out = with_client(make_cancel_action(target.text), allow_login)
    msgs = _print_messages(out.get("messages", []))
    text = {
        "cancelled": f"Cancelled: {target.text}.{msgs}\n  (Double-checked: it's gone from My Reservations.)",
        "failed": f"Not cancelled: {target.text}.{msgs or chr(10) + '  It still shows on My Reservations.'}",
        "unclear": f"Not sure it worked: {target.text}.{msgs}\n  Check `utcourts mine`.",
        "not_found": f"That reservation isn't on My Reservations anymore: {target.text}.",
    }[out["status"]]
    _emit(args, {**base, **out}, text)
    return 0 if out["status"] == "cancelled" else NOTHING_DONE


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
TOP_HELP = """\
Check, book and cancel UT RecSports courts from the terminal.

Sign in once with `utcourts login`; after that it re-signs itself in quietly
until UT's single sign-on wants your EID + Duo again.
"""

TOP_EPILOG = """\
examples:
  utcourts squash tomorrow --after 6pm      (check is the default command)
  utcourts check rb fri --grid
  utcourts book gre-squash tomorrow 9pm --court B
  utcourts mine
  utcourts cancel 1

  utcourts help <command>                   more on one command

sports:  squash (sq) · racquetball (rb, handball) · pickleball (pb) · tennis
dates:   today · tomorrow · fri · +2 · 11/20 · 2026-11-20   (today through 7 days out)
times:   9pm · 21 · 21:00

exit codes: 0 ok · 1 bad input · 2 needs a fresh sign-in · 3 network trouble or page changed
            4 nothing done (not open, not confirmed, ambiguous, or RecSports said no)
"""

COMMANDS = ("check", "book", "reserve", "mine", "reservations", "cancel", "login", "status",
            "logout", "facilities", "help")


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    fmt = argparse.RawDescriptionHelpFormatter

    def wrap(text: str) -> str:
        return textwrap.fill(text, 78)
    p = argparse.ArgumentParser(prog="utcourts", description=TOP_HELP, epilog=TOP_EPILOG, formatter_class=fmt)
    p.add_argument("-V", "--version", action="version", version=f"utcourts {__version__}")
    sub = p.add_subparsers(dest="cmd", title="commands", metavar="<command>")
    subs: dict[str, argparse.ArgumentParser] = {}

    def no_login(sp):
        sp.add_argument("--no-login", dest="login", action="store_false",
                        help="never open a sign-in window; exit 2 instead (automatic when not in a terminal)")

    c = sub.add_parser("check", help="show open courts (the default command)", formatter_class=fmt,
                       description=wrap("Show open courts. Words can be a sport, a facility short name, or a "
                                   "date, in any order."),
                       epilog="examples:\n  utcourts check squash tomorrow --after 6pm\n"
                              "  utcourts check rb gre-squash fri --before 9pm\n"
                              "  utcourts check tennis --days 3 --json\n"
                              "  utcourts check gre-rb +2 --grid")
    c.add_argument("terms", nargs="*", help="sport / facility / date words")
    c.add_argument("-s", "--sport", action="append", help="sport or facility (repeatable)")
    c.add_argument("-d", "--date", help="date (default: today)")
    c.add_argument("--days", type=int, default=1, help="look at this many days starting from the date")
    c.add_argument("--after", help="only slots starting at/after this time (e.g. 17, 5pm)")
    c.add_argument("--before", help="only slots starting before this time (e.g. 21, 9pm)")
    c.add_argument("--grid", action="store_true", help="show the full grid, not just open courts")
    c.add_argument("--all", action="store_true", help="--grid, and in --json include every slot")
    c.add_argument("--json", action="store_true", help="machine-readable output")
    no_login(c)
    c.set_defaults(func=cmd_check)
    subs["check"] = c

    b = sub.add_parser("book", aliases=["reserve"], help="book one open court (asks before booking)",
                       formatter_class=fmt,
                       description=wrap("Book one open court. You give the facility (or sport), the day and the "
                                   "start time; if more than one court fits, it asks (or add --court). "
                                   "It always shows exactly what it's about to book and asks y/N first, "
                                   "then re-checks the grid to make sure it worked."),
                       epilog="examples:\n  utcourts book gre-squash tomorrow 9pm --court B\n"
                              "  utcourts book squash fri 6pm              (pick from a list)\n"
                              "  utcourts book rsc-rb +2 18:00 --dry-run    (show it, don't book)\n\n"
                              "scripts/agents: no prompts without a terminal, so pass --yes — only after\n"
                              "the person has confirmed that exact slot.")
    b.add_argument("terms", nargs="*", help="facility/sport, date and time words, in any order")
    b.add_argument("--court", help="court name as shown on the grid (1, A, B1A, ...)")
    b.add_argument("--at", help="start time (instead of a time word)")
    b.add_argument("-d", "--date", help="date (instead of a date word)")
    b.add_argument("-y", "--yes", action="store_true", help="don't ask; book it")
    b.add_argument("-n", "--dry-run", action="store_true", help="show what would be booked, then stop")
    b.add_argument("--json", action="store_true", help="machine-readable output")
    no_login(b)
    b.set_defaults(func=cmd_book)
    subs["book"] = subs["reserve"] = b

    m = sub.add_parser("mine", aliases=["reservations"], help="list your reservations", formatter_class=fmt,
                       description=wrap("List your reservations from the My Reservations page, numbered for `cancel`."))
    m.add_argument("--json", action="store_true", help="machine-readable output")
    no_login(m)
    m.set_defaults(func=cmd_mine)
    subs["mine"] = subs["reservations"] = m

    x = sub.add_parser("cancel", help="cancel one of your reservations (asks first)", formatter_class=fmt,
                       description=wrap("Cancel a reservation. Pick it by its number from `utcourts mine`, or by "
                                   "words that appear in it. It asks y/N first, then checks it's gone."),
                       epilog="examples:\n  utcourts cancel 1\n  utcourts cancel --match \"11/18 9:00 PM\"\n"
                              "  utcourts cancel            (pick from a list)")
    x.add_argument("which", nargs="?", help="reservation number from `utcourts mine`")
    x.add_argument("--match", help="words that appear in the reservation (e.g. a date and time)")
    x.add_argument("-y", "--yes", action="store_true", help="don't ask; cancel it")
    x.add_argument("-n", "--dry-run", action="store_true", help="show what would be cancelled, then stop")
    x.add_argument("--json", action="store_true", help="machine-readable output")
    no_login(x)
    x.set_defaults(func=cmd_cancel)
    subs["cancel"] = x

    lg = sub.add_parser("login", help="sign in (opens a browser window for your EID + Duo)")
    lg.set_defaults(func=cmd_login)
    subs["login"] = lg

    st = sub.add_parser("status", help="is the saved session still good? (also works as a keep-alive)")
    st.add_argument("--refresh", action="store_true", help="if expired, try a quiet SSO sign-in")
    st.add_argument("-q", "--quiet", action="store_true")
    st.set_defaults(func=cmd_status)
    subs["status"] = st

    fa = sub.add_parser("facilities", help="list facilities and their short names")
    fa.add_argument("--json", action="store_true")
    fa.set_defaults(func=cmd_facilities)
    subs["facilities"] = fa

    lo = sub.add_parser("logout", help="delete the saved session and browser profile")
    lo.set_defaults(func=cmd_logout)
    subs["logout"] = lo

    h = sub.add_parser("help", help="show help for a command")
    h.add_argument("topic", nargs="?")
    h.set_defaults(func=None)
    subs["help"] = h
    return p, subs


def main(argv: list[str] | None = None) -> int:
    parser, subs = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "-V", "--version")):
        argv = ["check", *argv]  # `utcourts squash tomorrow` == `utcourts check squash tomorrow`
    args = parser.parse_args(argv)
    if args.cmd == "help":
        (subs.get(args.topic) or parser).print_help()
        return 0
    try:
        return args.func(args)
    except UTCourtsError as e:
        log(f"utcourts: {e}")
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
