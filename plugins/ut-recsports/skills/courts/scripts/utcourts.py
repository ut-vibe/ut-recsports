#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "playwright>=1.45",
#   "keyring>=24",
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
* The EID and password you type in the sign-in window are kept in the system
  keychain (the macOS Keychain on a Mac). Then when UT asks for your password
  again, the hidden browser types it in. If Duo still trusts the browser that's
  the end of it; if not, you approve a Duo push on your phone. No window either
  way. `login --no-save-password` turns this off and forgets the password.

Human output goes to the terminal; `--json` output and exit codes are for scripts
and agents (documented in SKILL.md next to this script).
"""
from __future__ import annotations

__version__ = "1.2.0"

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from itertools import groupby
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
ACCOUNT_FILE = HOME / "account.json"   # {"eid", "save_password"?}; the password itself is in the keychain
KEYRING_SERVICE = "utcourts"

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


@dataclass(frozen=True)
class Facility:
    id: int
    sports: tuple[str, ...]
    name: str                  # as RecSports names it (used in --json)
    aliases: tuple[str, ...]   # short names you can type; the first is the main one
    short: str                 # display name in human output


FACILITIES: list[Facility] = [
    Facility(28, ("squash",), "Bellmont Hall (BEL) - Squash", ("bel-squash", "bel"), "BEL Squash"),
    Facility(30, ("pickleball",), "Caven-Clark Courts (CCF) - Pickleball Only", ("ccf-pickleball", "ccf"),
             "CCF Pickleball"),
    Facility(35, ("racquetball",), "Gregory Gym (GRE) - Racquetball / Handball",
             ("gre-rb", "gre-racquetball", "gre-handball"), "GRE Racquetball/Handball"),
    Facility(40, ("squash",), "Gregory Gym (GRE) - Squash", ("gre-squash",), "GRE Squash"),
    Facility(55, ("squash",), "Recreational Sports Center (RSC) - Squash", ("rsc-squash",), "RSC Squash"),
    Facility(50, ("racquetball",), "Recreational Sports Center (RSC) - Racquetball / Handball",
             ("rsc-rb", "rsc-racquetball", "rsc-handball"), "RSC Racquetball/Handball"),
    Facility(60, ("tennis",), "Whitaker Courts (WC) - Tennis Only", ("wc-tennis",), "Whitaker Tennis"),
    Facility(65, ("pickleball", "tennis"), "Whitaker Courts (WC) - Pickleball / Tennis",
             ("wc-pickleball", "wc-mixed", "wc"), "Whitaker Pickleball/Tennis"),
]
FAC_BY_ID = {f.id: f for f in FACILITIES}

SPORT_ALIASES = {
    "squash": "squash", "sq": "squash",
    "racquetball": "racquetball", "rb": "racquetball", "racquet": "racquetball",
    "handball": "racquetball", "hb": "racquetball",
    "pickleball": "pickleball", "pb": "pickleball", "pickle": "pickleball",
    "tennis": "tennis", "tn": "tennis",
}

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class UTCourtsError(Exception):
    """A problem to show the person: the message, plus an optional hint on what to do."""
    exit_code = 1
    default_hint: str | None = None

    def __init__(self, message: str = "", hint: str | None = None):
        super().__init__(message)
        self.hint = hint if hint is not None else self.default_hint


class SignInNeeded(UTCourtsError):
    exit_code = 2
    default_hint = "Run utcourts login to sign in with your EID + Duo."


class DuoFailed(SignInNeeded):
    default_hint = "Run utcourts login to try again."


class PageChanged(UTCourtsError):
    exit_code = 3
    default_hint = "RecSports may have changed its pages; utcourts may need an update."


class NetworkTrouble(UTCourtsError):
    exit_code = 3
    default_hint = "Check your connection and try again."


# --------------------------------------------------------------------------- #
# Terminal output: colors, a spinner, stderr logging
# --------------------------------------------------------------------------- #
def _color_on(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    try:
        return stream.isatty() and os.environ.get("TERM") != "dumb"
    except (AttributeError, ValueError):
        return False


class Paint:
    """p = Paint(sys.stdout); p("text", "bold", "green"). Plain text unless that stream is a terminal."""
    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
             "blue": "34", "magenta": "35", "cyan": "36"}

    def __init__(self, stream=None):
        self.on = _color_on(stream if stream is not None else sys.stdout)

    def __call__(self, text: str, *styles: str) -> str:
        if not self.on or not styles or not text:
            return text
        return "".join(f"\033[{self.CODES[s]}m" for s in styles) + text + "\033[0m"

    def ok(self, text: str) -> str:
        return f"{self('✓', 'green', 'bold')} {text}"

    def bad(self, text: str) -> str:
        return f"{self('✗', 'red', 'bold')} {text}"

    def warn(self, text: str) -> str:
        return f"{self('!', 'yellow', 'bold')} {text}"

    def note(self, text: str) -> str:
        return f"{self('›', 'cyan', 'bold')} {text}"


class Spinner:
    """`with Spinner("Checking…"):` shows a spinner on stderr while waiting (terminals only).
    log() prints above it; Spinner.say() changes its text."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    current: "Spinner | None" = None

    def __init__(self, text: str, enabled: bool = True):
        self.text = text
        try:
            tty = sys.stderr.isatty()
        except (AttributeError, ValueError):
            tty = False
        self.enabled = enabled and tty and os.environ.get("TERM") != "dumb" and Spinner.current is None
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Spinner":
        if self.enabled:
            Spinner.current = self
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def _spin(self) -> None:
        p = Paint(sys.stderr)
        i = 0
        while not self._stop.wait(0.08):
            with self.lock:
                sys.stderr.write(f"\r\033[2K{p(self.FRAMES[i % len(self.FRAMES)], 'cyan')} {p(self.text, 'dim')}")
                sys.stderr.flush()
            i += 1

    def __exit__(self, *exc) -> bool:
        if self.enabled:
            self._stop.set()
            if self._thread:
                self._thread.join()
            with self.lock:
                sys.stderr.write("\r\033[2K")
                sys.stderr.flush()
            Spinner.current = None
        return False

    @staticmethod
    def say(text: str) -> bool:
        """Change the running spinner's text. False if no spinner is showing."""
        if Spinner.current is None:
            return False
        Spinner.current.text = text
        return True


def log(msg: str) -> None:
    sp = Spinner.current
    if sp is None:
        print(msg, file=sys.stderr, flush=True)
        return
    with sp.lock:
        sys.stderr.write("\r\033[2K" + msg + "\n")
        sys.stderr.flush()


def tell(msg: str) -> None:
    """A passing status: the spinner's text if one is showing, else its own line."""
    if not Spinner.say(msg):
        log(Paint(sys.stderr).note(msg))


def someone_watching() -> bool:
    """True if stderr is a terminal, so a message printed now is seen now."""
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


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
        raise UTCourtsError(f"“{text}” isn't a real date.") from e
    return None


TIME_HINT = "Try 9pm, 9:30pm or 21:00."


def parse_clock(text: str) -> int:
    """'17', '17:30', '5pm', '5:30 pm', '5p' -> minutes after midnight."""
    t = text.strip().lower().replace(" ", "").replace(".", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm|a|p)?", t)
    if not m:
        raise UTCourtsError(f"Couldn't read the time “{text}”", hint=TIME_HINT)
    h, mins, ampm = int(m[1]), int(m[2] or 0), m[3]
    if ampm:
        if not 1 <= h <= 12:
            raise UTCourtsError(f"Couldn't read the time “{text}”", hint=TIME_HINT)
        h = h % 12 + (12 if ampm.startswith("p") else 0)
    if not (0 <= h <= 24 and 0 <= mins < 60):
        raise UTCourtsError(f"Couldn't read the time “{text}”", hint=TIME_HINT)
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
            raise UTCourtsError(f"Unknown sport or facility “{raw}”", hint="See the list: utcourts facilities")
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
    if browser_cookies is None:
        browser_cookies = (load_session() or {}).get("browser_cookies", [])
    _write_private(SESSION_FILE, {"saved_at": datetime.now().isoformat(timespec="seconds"),
                                  "user_agent": user_agent or DEFAULT_UA, "cookies": cookies,
                                  "browser_cookies": browser_cookies})


def _write_private(path: Path, data: dict) -> None:
    """Write JSON that only this user can read (0600, in a 0700 folder)."""
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(HOME, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Saved password (on unless `login --no-save-password`). The EID and the setting live in
# account.json; the password lives in the system keychain via the `keyring` package.
# --------------------------------------------------------------------------- #
def load_account() -> dict:
    try:
        data = json.loads(ACCOUNT_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_account(data: dict) -> None:
    _write_private(ACCOUNT_FILE, data)


def saving_passwords() -> bool:
    """True unless turned off with `login --no-save-password` (then save_password is False)."""
    return load_account().get("save_password", True) is not False


def can_save_passwords() -> bool:
    return saving_passwords() and keyring_problem() is None


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def keychain_name() -> str:
    if sys.platform == "darwin":
        return "the macOS Keychain"
    if os.name == "nt":
        return "Windows Credential Manager"
    return "your system keyring"


def keyring_problem() -> str | None:
    """Why a password can't be saved here, or None if it can."""
    kr = _keyring()
    if kr is None:
        return "Saving a password needs the keyring package (uv installs it; or: pip install keyring)."
    try:
        from keyring.backends import fail
        if isinstance(kr.get_keyring(), fail.Keyring):
            return "This computer has no system keyring for utcourts to keep a password in."
    except Exception as ex:
        return f"Couldn't open {keychain_name()}: {ex}"
    return None


def saved_credentials() -> tuple[str, str] | None:
    """The saved (EID, password), or None."""
    eid = load_account().get("eid")
    kr = _keyring() if eid and saving_passwords() else None
    if kr is None:
        return None
    try:
        password = kr.get_password(KEYRING_SERVICE, eid)
    except Exception:
        return None
    return (eid, password) if password else None


def store_credentials(eid: str, password: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    acct = load_account()
    try:
        kr.set_password(KEYRING_SERVICE, eid, password)
    except Exception as ex:
        log(Paint(sys.stderr).warn(f"Couldn't save your password in {keychain_name()}: {ex}"))
        return False
    if acct.get("eid") and acct["eid"] != eid:
        _delete_password(kr, acct["eid"])
    acct["eid"] = eid
    save_account(acct)
    return True


def _delete_password(kr, eid: str) -> bool:
    try:
        kr.delete_password(KEYRING_SERVICE, eid)
        return True
    except Exception:
        return False


def forget_password() -> bool:
    """Remove the saved password (the EID and the setting stay)."""
    eid, kr = load_account().get("eid"), _keyring()
    return bool(eid and kr and _delete_password(kr, eid))


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
    hidden browser and run it there; if UT wants a human, open a window (when allowed).

    A saved password is only typed in when allow_login is set (someone is at the terminal),
    because it can set off a Duo push, and a push nobody is expecting is a bad habit to teach."""
    sess = load_session()
    creds = saved_credentials() if allow_login else None
    tried_quiet = False
    if sess and sess.get("cookies"):
        client = HttpClient(sess)
        try:
            return action(client)
        except SignInNeeded:
            pass
        finally:
            client.close()
        tell("RecSports session expired. Trying to sign back in quietly…")
    if (sess and sess.get("cookies")) or PROFILE_DIR.exists():
        ok, value = browser_session(interactive=False, timeout_s=120 if creds else 30, action=action, creds=creds)
        tried_quiet = True
        if ok:
            return value
    elif not allow_login:
        raise SignInNeeded("Not signed in yet.")
    if allow_login:
        return interactive_login(action, quiet_first=not tried_quiet)
    raise SignInNeeded("Your UT sign-in has expired.", hint=sign_in_hint())


def sign_in_hint() -> str:
    if saved_credentials():
        return "Run utcourts login. It types in your saved password; approve the Duo push if one comes."
    return SignInNeeded.default_hint


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
                raise UTCourtsError("The utcourts browser profile is already in use.",
                                    hint="Is another utcourts login window still open?") from e
    raise UTCourtsError(
        "Couldn't start a browser for sign-in.",
        hint="Install Google Chrome, or run: uvx playwright install chromium\n"
             f"({str(last).splitlines()[0] if last else 'unknown error'})")


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
        if any(_is_duo(urlparse(fr.url or "").hostname) for fr in page.frames):
            return True
        return _password_box(page) is not None
    except Exception:
        return False


def _is_duo(host: str | None) -> bool:
    return "duo" in (host or "")  # api-xxxx.duosecurity.com


def _password_box(page):
    box = page.query_selector("input[type=password]")
    return box if box and box.is_visible() else None


def _fill_sign_in(page, box, creds: tuple[str, str]) -> None:
    eid, password = creds
    user = page.query_selector("input[name=j_username], input#username")
    if user and user.is_visible() and user.is_editable():
        user.fill(eid)
    box.fill(password)
    box.press("Enter")  # submits with the form's own "Sign in" button


def _sign_in_error(page) -> str:
    """The sign-in page's own error line, if it shows one."""
    for el in page.query_selector_all(".form-error, .output--error, .error, .alert, [role=alert]"):
        try:
            text = " ".join(el.inner_text().split())
        except Exception:
            continue
        if text and el.is_visible():
            return text
    return ""


def _watch_sign_in_form(ctx) -> dict:
    """Remember the EID and password sent to UT's sign-in form in this browser, so they can be
    saved once the sign-in works. Reads the form post itself, so nothing is injected into the page."""
    typed: dict = {}

    def on_request(req) -> None:
        try:
            if req.method != "POST" or urlparse(req.url).hostname == APP_HOST:
                return
            body = req.post_data or ""
        except Exception:
            return
        if "j_password=" not in body:
            return
        q = parse_qs(body)
        eid, password = (q.get("j_username") or [""])[0].strip(), (q.get("j_password") or [""])[0]
        if eid and password:
            typed.update(eid=eid, password=password)

    ctx.on("request", on_request)
    return typed


def _save_typed(typed: dict | None, creds: tuple[str, str] | None) -> None:
    if not typed or not saving_passwords():
        return
    problem = keyring_problem()
    if problem:
        if load_account().get("save_password") is True:  # asked for with --save-password
            log(Paint(sys.stderr).warn(problem))
        return
    pair = (typed["eid"], typed["password"])
    if pair != creds and store_credentials(*pair):
        log(Paint(sys.stderr).ok(f"Saved your EID and password in {keychain_name()}"))


# Duo's Universal Prompt (api-xxxx.duosecurity.com/frame/frameless/v4/…). Read by its
# words as well as its ids so a redesign degrades to "open the window", not to a hang.
_DUO_TRUST = re.compile(r"is this your device|trust this browser", re.I)
_DUO_FAILED = re.compile(r"\b(denied|declined|timed out|locked out|expired)\b", re.I)
_DUO_PUSHED = re.compile(r"check for a duo push|sent to\b|approve (the|this) (notification|request|push)", re.I)
_DUO_CODE_WORDS = re.compile(r"enter (the |this )?(verification )?code", re.I)


def _visible(page, selector: str):
    el = page.query_selector(selector)
    return el if el and el.is_visible() else None


def _duo_screen(page) -> tuple[str, str]:
    """What Duo is showing: trust / code / failed / push / other, plus a detail."""
    if _visible(page, "#trust-browser-button"):
        return "trust", ""
    code = _visible(page, ".verification-code")
    if code:
        return "code", code.inner_text().strip()
    text = page.inner_text("body")
    if _DUO_TRUST.search(text):
        return "trust", ""
    if _DUO_CODE_WORDS.search(text):
        m = re.search(r"^\s*(\d{3,6})\s*$", text, re.M)
        if m:
            return "code", m.group(1)
    bad = next((" ".join(line.split()) for line in text.splitlines() if _DUO_FAILED.search(line)), "")
    if bad:
        return "failed", bad
    if _visible(page, "input#passcode-input, input[name=passcode]"):
        return "other", ""
    if _DUO_PUSHED.search(text):
        return "push", ""
    return "other", ""


def _click_first(page, *locators) -> bool:
    from playwright.sync_api import Error as PWError
    for loc in locators:
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3000)
                return True
        except PWError:
            pass
    return False


class _DuoWatch:
    """Gets a hidden-browser sign-in through Duo: tells the person about the push (and the
    code, for Verified Push), and says yes to "trust this browser" (it's utcourts' own
    profile). step() returns False when a person has to see the page after all."""

    def __init__(self):
        self.progress_at: float | None = None  # last time Duo showed something we understood
        self.announced: str | None = None
        self.trust_at = 0.0
        self.nudges = 0

    def step(self, page) -> bool:
        now = time.monotonic()
        if self.progress_at is None:
            self.progress_at = now  # Duo just showed up
        state, detail = _duo_screen(page)
        e = Paint(sys.stderr)
        if state == "failed":
            raise DuoFailed(f"Duo: {detail}")
        if state == "trust":
            if now - self.trust_at > 2:
                self.trust_at = now
                _click_first(page, page.locator("#trust-browser-button"),
                             page.get_by_role("button", name=re.compile(r"^\s*(yes|trust)", re.I)))
        elif state == "code":
            if not someone_watching():
                return False  # the code has to be read off a screen; the window will show it
            if self.announced != detail:
                self.announced = detail
                log(e.note(f"Duo code {e(detail, 'bold')}: approve the push on your phone and type in that code."))
                Spinner.say("Waiting for Duo…")
        elif state == "push":
            if self.announced is None:
                self.announced = "push"
                log(e.note("Approve the Duo push on your phone."))
                Spinner.say("Waiting for Duo…")
        else:  # still loading, a list of methods, a passcode box, or something new
            if now - self.progress_at > 15:
                return False
            if now - self.progress_at > 4 and self.nudges < 2:
                push = re.compile(r"duo push|send (me )?a push", re.I)
                other = re.compile(r"^\s*other options\s*$", re.I)
                if _click_first(page, page.get_by_role("button", name=push), page.get_by_role("link", name=push),
                                page.get_by_role("button", name=other), page.get_by_role("link", name=other)):
                    self.nudges += 1
                    self.progress_at = now
            return True
        self.progress_at = now
        return True


def browser_session(interactive: bool, timeout_s: float, action=None,
                    creds: tuple[str, str] | None = None, fresh: bool = False) -> tuple[bool, object]:
    """Open the private profile and get to the court page (signing in via SSO if needed).

    creds: a saved (EID, password). When UT shows its password form they're typed in, once.
    If UT turns them down, the password is forgotten, so a stale one can't lock the account.
    In the hidden browser, creds also mean Duo may be used: the push is announced on stderr
    and "trust this browser" is answered yes. Without creds, the hidden browser gives up as
    soon as UT wants a password or Duo.

    fresh: start signed out of RecSports and UT's SSO (Duo's cookies stay, so a trusted
    browser stays trusted), so UT asks for the password. For `login --save-password`.

    On success: saves the cookies for the fast path, runs `action(BrowserClient)` from inside
    the browser (so it works even if exported cookies ever stop working), and returns
    (True, its result). Returns (False, None) if a human is needed. Raises DuoFailed if Duo
    said no (denied, timed out).
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except ImportError as e:
        raise UTCourtsError("Playwright isn't installed.",
                            hint="Run it with uv (uv run utcourts.py …), or: pip install playwright") from e

    with sync_playwright() as pw:
        ctx = _launch(pw, headless=not interactive)
        try:
            restore_browser_cookies(ctx)
            if fresh:
                duo_cookies = [c for c in ctx.cookies() if _is_duo(c.get("domain", "").lstrip("."))]
                ctx.clear_cookies()
                if duo_cookies:
                    ctx.add_cookies([{k: c[k] for k in _PW_COOKIE_KEYS if k in c} for c in duo_cookies])
            typed = _watch_sign_in_form(ctx) if saving_passwords() else None
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ua = None
            try:
                ua = page.evaluate("navigator.userAgent")
            except PWError:
                pass
            if ua and "Headless" in ua:
                ua = ua.replace("HeadlessChrome", "Chrome")
                ctx.set_extra_http_headers({"User-Agent": ua})
                try:  # the page's own JS sees it too (Duo looks at the browser)
                    ctx.new_cdp_session(page).send("Emulation.setUserAgentOverride", {"userAgent": ua})
                except Exception:
                    pass
            try:
                page.goto(COURTS_URL, wait_until="domcontentloaded", timeout=30_000)
            except PWError as e:
                if "net::" in str(e) and not interactive:
                    raise NetworkTrouble(f"Couldn't reach {APP_HOST}: {str(e).splitlines()[0]}") from e

            deadline = time.monotonic() + timeout_s
            human_since: float | None = None
            last_url, url_since = "", time.monotonic()
            nudged = False
            filled_at: float | None = None  # when the saved password was typed in
            duo = _DuoWatch()
            while time.monotonic() < deadline:
                if page.is_closed():
                    if interactive:
                        raise SignInNeeded("The sign-in window closed before sign-in finished.", hint="Run utcourts login to try again.")
                    return False, None
                try:
                    url = page.url
                    host = urlparse(url).hostname
                    if url != last_url:
                        last_url, url_since = url, time.monotonic()
                    if host == APP_HOST and page.query_selector("#form_courts"):
                        remember_browser(ctx, ua or page.evaluate("navigator.userAgent"))
                        _save_typed(typed, creds)
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
                    box = _password_box(page) if creds and host != APP_HOST and not _is_duo(host) else None
                    if box and filled_at is None:
                        if not interactive:
                            tell("Signing in with your saved password…")
                        _fill_sign_in(page, box, creds)
                        filled_at = time.monotonic()
                        continue
                    if box and time.monotonic() - filled_at > 1.5 and not box.input_value():
                        # UT showed the form again: it turned the password down. Never retry it.
                        why = _sign_in_error(page)
                        forget_password()
                        creds = None
                        e = Paint(sys.stderr)
                        log(e.warn("UT didn't accept the saved password" + (f" ({why})" if why else "")
                                   + f", so utcourts removed it from {keychain_name()}."))
                        log("  It saves the new one the next time you sign in in the window.")
                        if not interactive:
                            return False, None
                        continue
                    if not interactive:
                        if creds and _is_duo(host):
                            if not duo.step(page):
                                return False, None
                            human_since = None
                        elif creds and filled_at and time.monotonic() - filled_at < 10:
                            pass  # the saved password is on its way to UT
                        else:
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


def interactive_login(action=None, quiet_first: bool = True, fresh: bool = False):
    """Sign in for someone who asked to: with a saved password, in the hidden browser (only
    Duo needs them); otherwise, or if that can't finish, in a visible Chrome window.
    fresh: make UT ask for the password in the window (see browser_session)."""
    e = Paint(sys.stderr)
    home = str(HOME).replace(str(Path.home()), "~", 1)
    creds = saved_credentials()
    if creds and quiet_first:
        with Spinner("Signing in with your saved password…"):
            ok, value = browser_session(interactive=False, timeout_s=120, action=action, creds=creds)
        if ok:
            log(e.ok("Signed in") + e(f" · session saved in {home}", "dim"))
            return value
        creds = saved_credentials()  # gone if UT turned it down
    if creds:
        log(e.note("Opening a Chrome window to finish signing in. Your saved EID and password are filled in."))
    else:
        log(e.note("Opening a Chrome window. Sign in there with your UT EID and Duo."))
        if can_save_passwords():
            log(e(f"  The EID and password you type there will be saved in {keychain_name()}, so next time", "dim"))
            log(e("  only Duo needs you. Don't want that? utcourts login --no-save-password", "dim"))
    log(e("  If Duo offers “Trust this browser”, say yes and you'll stay signed in longer.", "dim"))
    waiting = "Waiting for you to sign in…"
    if Spinner.say(waiting):
        ok, value = browser_session(interactive=True, timeout_s=300, action=action, creds=creds, fresh=fresh)
    else:
        with Spinner(waiting):
            ok, value = browser_session(interactive=True, timeout_s=300, action=action, creds=creds, fresh=fresh)
    if not ok:
        raise SignInNeeded("Didn't finish signing in within 5 minutes.", hint="Run utcourts login to try again.")
    log(e.ok("Signed in") + e(f" · session saved in {home}", "dim"))
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
    raise UTCourtsError("This reservation can only be cancelled on the website.", hint=MINE_URL)


# --------------------------------------------------------------------------- #
# Output: formatting helpers
# --------------------------------------------------------------------------- #
def _mins(t: str) -> int:
    """'21:00' -> 1260"""
    return int(t[:2]) * 60 + int(t[3:])


def fmt_time(minutes: int) -> str:
    """1260 -> '9 PM', 1290 -> '9:30 PM'"""
    h, m = divmod(minutes, 60)
    suffix = "PM" if 12 <= h < 24 else "AM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}" if m else f"{h12} {suffix}"


def fmt_day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def day_token(d: date) -> str:
    """The shortest date word that `book` reads back as this day."""
    delta = (d - austin_today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 1 < delta < 7:
        return f"{d:%a}".lower()
    return f"{d.month}/{d.day}"


def time_token(t: str) -> str:
    return fmt_time(_mins(t)).replace(" ", "").lower()


def court_key(court: str) -> list[tuple]:
    """Natural order: 9 before 10, B1A before B2A."""
    return [(0, int(x)) if x.isdigit() else (1, x.lower()) for x in re.split(r"(\d+)", court) if x]


def fac_short(facility_id: int, fallback: str) -> str:
    f = FAC_BY_ID.get(facility_id)
    return f.short if f else fallback


def fac_alias(facility_id: int) -> str | None:
    f = FAC_BY_ID.get(facility_id)
    return f.aliases[0] if f else None


def fac_title(p: Paint, facility_id: int, facility: str) -> str:
    alias = fac_alias(facility_id)
    return p(fac_short(facility_id, facility), "bold") + (f"  {p(alias, 'dim')}" if alias else "")


def slot_desc(facility_id: int, facility: str, day_iso: str, t: str, court: str) -> str:
    return (f"{fac_short(facility_id, facility)} · court {court} · "
            f"{fmt_day(date.fromisoformat(day_iso))} · {fmt_time(_mins(t))}")


def window_text(after: int | None, before: int | None) -> str | None:
    if after is not None and before is not None:
        return f"{fmt_time(after)} – {fmt_time(before)}"
    if after is not None:
        return f"from {fmt_time(after)}"
    if before is not None:
        return f"before {fmt_time(before)}"
    return None


def day_title(d: date, label: str | None = None, after: int | None = None, before: int | None = None) -> str:
    rel = {0: "Today", 1: "Tomorrow"}.get((d - austin_today()).days)
    parts = [label] if label else []
    parts.append(f"{rel}, {fmt_day(d)}" if rel else fmt_day(d))
    w = window_text(after, before)
    if w:
        parts.append(w)
    return " · ".join(parts)


SPORT_TITLES = {"squash": "Squash", "racquetball": "Racquetball & handball",
                "pickleball": "Pickleball", "tennis": "Tennis"}


def sport_label(facilities: list[Facility]) -> str | None:
    ids = {f.id for f in facilities}
    if ids == set(FAC_BY_ID):
        return "All courts"
    for sport, title in SPORT_TITLES.items():
        if ids == {f.id for f in FACILITIES if sport in f.sports}:
            return title
    return None


def _courts_line(p: Paint, courts: list[str]) -> str:
    return "  ".join(p(c, "green") for c in sorted(courts, key=court_key))


def _nothing_left(r: DayResult) -> str:
    return ("nothing left to book today" if r.date == austin_today().isoformat()
            else "nothing bookable this day")


def _in_window(t: str, after: int | None, before: int | None) -> bool:
    m = _mins(t)
    return (after is None or m >= after) and (before is None or m < before)


# --------------------------------------------------------------------------- #
# Output: check
# --------------------------------------------------------------------------- #
def print_open(results: list[DayResult], after: int | None, before: int | None,
               label: str | None = None) -> None:
    p = Paint(sys.stdout)
    out: list[str] = []
    first: tuple[DayResult, str, str] | None = None
    windowed = after is not None or before is not None
    for day_iso, group in groupby(results, key=lambda r: r.date):
        rows_by_fac = [(r, r.open_by_time(after, before)) for r in group]
        tw = max([len(fmt_time(_mins(x["time"]))) for _, rows in rows_by_fac for x in rows] or [0])
        if out:
            out.append("")
        out.append(p(day_title(date.fromisoformat(day_iso), label, after, before), "bold"))
        empty = [r for r, rows in rows_by_fac if not rows]
        if len(rows_by_fac) > 1 and len(empty) == len(rows_by_fac):
            # several facilities, none open: one line, not a section each
            today_over = all(not r.courts for r in empty) and day_iso == austin_today().isoformat()
            out += ["", p("  " + ("Nothing left to book today" if today_over else
                                  "No open courts" + (" in that window" if windowed else "")), "dim")]
            continue
        for r, rows in rows_by_fac:
            if not rows and len(rows_by_fac) > 1:
                continue  # summarized below
            out += ["", "  " + fac_title(p, r.facility_id, r.facility)]
            if not r.courts:
                out.append(p(f"    {_nothing_left(r)}", "dim"))
            elif not rows:
                out.append(p("    no open courts" + (" in that window" if windowed else ""), "dim"))
            for x in rows:
                out.append(f"    {fmt_time(_mins(x['time'])):>{tw}}  {_courts_line(p, x['courts'])}")
                if first is None:
                    first = (r, x["time"], sorted(x["courts"], key=court_key)[0])
        if empty and len(rows_by_fac) > 1:
            out += ["", p("  Nothing open at " + ", ".join(fac_short(r.facility_id, r.facility) for r in empty), "dim")]
    print("\n".join(out))
    if first and fac_alias(first[0].facility_id):
        r, t, court = first
        print("\n" + p(f"Book one: utcourts book {fac_alias(r.facility_id)} "
                       f"{day_token(date.fromisoformat(r.date))} {time_token(t)} --court {court}", "dim"))
    elif first is None and results:
        print("\n" + p("Add --grid to see what's taken, or try another day.", "dim"))


GRID_SYMBOL = {"open": "✓", "reserved": "·", "event": "E", "priority": "P",
               "closed": "×", "yours": "★", "unavailable": "-"}
GRID_STYLE = {"open": ("green", "bold"), "reserved": ("dim",), "event": ("yellow",),
              "priority": ("magenta",), "closed": ("dim",), "yours": ("cyan", "bold"), "unavailable": ("dim",)}
GRID_WORD = {"open": "open", "reserved": "reserved", "yours": "yours", "event": "event",
             "priority": "faculty/staff priority", "closed": "closed", "unavailable": "other"}


def print_grid(results: list[DayResult], after: int | None, before: int | None) -> None:
    p = Paint(sys.stdout)
    sym = lambda st: p(GRID_SYMBOL.get(st, "?"), *GRID_STYLE.get(st, ()))  # noqa: E731
    blocks: list[str] = []
    seen: set[str] = set()
    for r in results:
        lines = [f"{fac_title(p, r.facility_id, r.facility)}  "
                 + p("· " + day_title(date.fromisoformat(r.date)), "dim")]
        by_time: dict[str, dict[str, Slot]] = {}
        for s in r.slots:
            by_time.setdefault(s.time, {})[s.court] = s
        times = [t for t in by_time if _in_window(t, after, before)]
        if not r.courts or not times:
            lines.append(p(f"  {_nothing_left(r)}" if not r.courts else "  no slots in that window", "dim"))
            blocks.append("\n".join(lines))
            continue
        tw = max(len(fmt_time(_mins(t))) for t in times)
        cw = max(2, *(len(c) for c in r.courts)) + 1
        lines.append(" " * (2 + tw) + p("".join(c.rjust(cw) for c in r.courts), "dim"))
        for t in times:
            row = f"  {fmt_time(_mins(t)):>{tw}}"
            for c in r.courts:
                s = by_time[t].get(c)
                if s is None:
                    row += " " * cw
                else:
                    seen.add(s.status)
                    row += " " * (cw - 1) + sym(s.status)
            lines.append(row)
        named = sorted({(s.status, re.sub(r"^Event Conflict:\s*", "", s.detail)) for s in r.slots
                        if s.status in ("event", "unavailable") and s.detail and _in_window(s.time, after, before)})
        if named:
            lines.append("")
            lines += [f"  {sym(st)} {detail}" for st, detail in named]
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
    legend = [f"{sym(st)} {GRID_WORD[st]}" for st in GRID_SYMBOL if st in seen]
    if legend:
        print("\n" + "   ".join(legend))


def as_json(results: list[DayResult], after: int | None, before: int | None, include_all: bool) -> str:
    out = []
    for r in results:
        item = {"date": r.date, "weekday": r.weekday, "facility_id": r.facility_id,
                "facility": r.facility, "courts": r.courts,
                "open": r.open_by_time(after, before)}
        if include_all:
            item["slots"] = [{k: v for k, v in asdict(s).items() if k != "href"} for s in r.slots
                             if _in_window(s.time, after, before)]
        out.append(item)
    return json.dumps({"generated_at": datetime.now(AUSTIN_TZ).isoformat(timespec="seconds"),
                       "source": COURTS_URL, "results": out}, indent=2)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _checking(facilities: list[Facility]) -> str:
    return f"Checking {facilities[0].short}…" if len(facilities) == 1 else f"Checking {len(facilities)} facilities…"


def cmd_check(args) -> int:
    today = austin_today()
    sport_terms: list[str] = list(args.sport or [])
    day: date | None = parse_date(args.date) if args.date else None
    if args.date and day is None:
        raise UTCourtsError(f"Couldn't read the date “{args.date}”", hint="Try today, tomorrow, fri, +2 or 11/20.")
    for term in args.terms:
        d = parse_date(term, today)
        if d is not None and term.lower() not in SPORT_ALIASES:
            if day is not None:
                raise UTCourtsError("Give just one date.", hint="For a range, add --days N.")
            day = d
        else:
            sport_terms.append(term)
    day = day or today
    facilities = resolve_facilities(sport_terms) if sport_terms else list(FACILITIES)

    last_ok = today + timedelta(days=MAX_DAYS_AHEAD)
    if day < today:
        raise UTCourtsError(f"{fmt_day(day)} is in the past.")
    if day > last_ok:
        raise UTCourtsError(f"RecSports only lists {MAX_DAYS_AHEAD} days ahead.",
                            hint=f"The last day you can check is {fmt_day(last_ok)}.")
    days = [day + timedelta(days=i) for i in range(max(1, args.days)) if day + timedelta(days=i) <= last_ok]
    e = Paint(sys.stderr)
    if args.days > len(days):
        log(e.warn(f"Showing through {fmt_day(last_ok)}, the last day RecSports lists."))

    after = parse_clock(args.after) if args.after else None
    before = parse_clock(args.before) if args.before else None
    jobs = [(d, f.id) for d in days for f in facilities]
    with Spinner(_checking(facilities), enabled=not args.json):
        results = get_results(jobs, allow_login=args.login and sys.stdin.isatty())

    # Sanity check: the site clamps dates it won't show instead of erroring.
    for (d, fid), r in zip(jobs, results):
        if r.date != d.isoformat():
            log(e.warn(f"Asked for {fmt_day(d)}, but RecSports returned {fmt_day(date.fromisoformat(r.date))}."))
        if r.facility_id not in (fid, -1):
            log(e.warn(f"Asked for facility {fid}, but RecSports returned {r.facility_id}."))

    if args.json:
        print(as_json(results, after, before, args.all))
    elif args.grid or args.all:
        print_grid(results, after, before)
    else:
        print_open(results, after, before, sport_label(facilities) if sport_terms else "All courts")
    return 0


def cmd_login(args) -> int:
    e = Paint(sys.stderr)
    acct = load_account()
    if args.save_password is True:
        problem = keyring_problem()
        if problem:
            raise UTCourtsError(problem)
        save_account({**acct, "save_password": True})
    elif args.save_password is False:
        forgot = forget_password()
        acct.pop("eid", None)
        save_account({**acct, "save_password": False})
        log(e.ok("utcourts won't save your password" + (f"; removed it from {keychain_name()}" if forgot else "")))
    saving = can_save_passwords()
    # Nothing saved yet: have UT ask for the password, so there's something to save.
    interactive_login(fresh=saving and saved_credentials() is None)
    # Make sure the quick (no-browser) path works with the saved cookies.
    try:
        with Spinner("Checking the saved session…"):
            fetch_many([(austin_today(), 35)])
    except SignInNeeded:
        e = Paint(sys.stderr)
        log(e.warn("RecSports didn't accept the saved cookies outside the browser."))
        log("  Checks still work; they go through a hidden browser, which is a bit slower.")
    if saving and not saved_credentials():
        log(e.warn("UT didn't ask for your password this time (you were still signed in), so it isn't saved yet."))
        log("  utcourts saves it the next time you sign in in the window.")
    return 0


def _saved_at(sess: dict) -> str:
    try:
        t = datetime.fromisoformat(sess.get("saved_at", ""))
    except ValueError:
        return "?"
    return f"{fmt_day(t.date())}, {fmt_time(t.hour * 60 + t.minute)}"


def cmd_status(args) -> int:
    p = Paint(sys.stdout)
    sess = load_session()

    password_saved = saved_credentials() is not None

    def report(signed_in: bool, text: str) -> None:
        if args.json:
            print(json.dumps({"signed_in": signed_in, "saved_at": (sess or {}).get("saved_at"),
                              "password_saved": password_saved}, indent=2))
        elif not (args.quiet and signed_in):
            print(text)

    if not sess:
        report(False, p.bad("Not signed in") + "\n  Run utcourts login to sign in with your EID + Duo.")
        return 2
    try:
        with Spinner("Checking the saved session…", enabled=not args.json):
            fetch_many([(austin_today(), 35)])
        report(True, p.ok("Signed in") + p(f" · session saved {_saved_at(sess)}"
                                            + (" · password saved" if password_saved else ""), "dim"))
        return 0
    except SignInNeeded:
        if args.refresh:
            creds = saved_credentials() if args.login and sys.stdin.isatty() else None
            with Spinner("Signing back in quietly…", enabled=not args.json):
                ok = browser_session(interactive=False, timeout_s=120 if creds else 30, creds=creds)[0]
            if ok:
                report(True, p.ok("Signed back in quietly"))
                return 0
            report(False, p.bad("Signed out: UT wants your EID + Duo again") + "\n  Run utcourts login.")
        else:
            report(False, p.bad("RecSports session expired")
                   + "\n  Run utcourts status --refresh to sign back in quietly, or utcourts login.")
        return 2


def cmd_logout(args) -> int:
    removed = []
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        removed.append("session")
    if PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        removed.append("browser profile")
    if ACCOUNT_FILE.exists():
        if forget_password():
            removed.append(f"password in {keychain_name()}")
        if saving_passwords():
            ACCOUNT_FILE.unlink()
        else:
            save_account({"save_password": False})  # someone who opted out stays opted out
    p = Paint(sys.stdout)
    if not removed:
        print("Already signed out.")
        return 0
    what = removed[0] if len(removed) == 1 else ", ".join(removed[:-1]) + " and " + removed[-1]
    print(p.ok("Signed out") + p(f" · removed the saved {what}", "dim"))
    return 0


FACILITY_GROUPS = [("Squash", "squash", "squash · sq"),
                   ("Racquetball & handball", "racquetball", "racquetball · rb · handball · hb"),
                   ("Pickleball", "pickleball", "pickleball · pb"),
                   ("Tennis", "tennis", "tennis · tn")]


def cmd_facilities(args) -> int:
    if args.json:
        print(json.dumps([{"id": f.id, "name": f.name, "sports": list(f.sports), "aliases": list(f.aliases)}
                          for f in FACILITIES], indent=2))
        return 0
    p = Paint(sys.stdout)
    w = max(len(f.aliases[0]) for f in FACILITIES)
    lines: list[str] = []
    for title, sport, words in FACILITY_GROUPS:
        if lines:
            lines.append("")
        lines.append(p(title, "bold") + "  " + p(words, "dim"))
        for f in FACILITIES:
            if sport in f.sports:
                also = [s for s in f.sports if s != sport]
                lines.append(f"  {f.aliases[0]:<{w}}  {f.name.split(' - ')[0]}"
                             + (p(f" · also {', '.join(also)}", "dim") if also else ""))
    print("\n".join(lines))
    print("\n" + p("Use a sport word for every facility that has it, or a short name for one.", "dim"))
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


BOOK_EXAMPLE = "Example: utcourts book gre-squash tomorrow 9pm --court B"


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
            raise UTCourtsError(f"Didn't understand “{t}”", hint=BOOK_EXAMPLE)
        if at is not None:
            raise UTCourtsError("Give just one time.")
        at = m
    return facs, day, at


def _find_slot(g: DayResult, t: str, court: str) -> Slot | None:
    return next((s for s in g.slots if s.time == t and s.court.lower() == court.lower()), None)


def _said(p: Paint, msgs: list[dict], quiet: bool = False) -> list[str]:
    """What RecSports itself said, one line each."""
    return [p(f"  RecSports: {m['text']}", "dim") if quiet else f"  {p('RecSports:', 'dim')} {m['text']}"
            for m in msgs]


def _taken_words(slots: list[Slot]) -> str:
    """'court 11 reserved' / 'courts A B reserved · court C event (Handball)'"""
    groups: dict[str, list[str]] = {}
    for s in slots:
        if s.status == "event":
            word = "event (" + re.sub(r"^Event Conflict:\s*", "", s.detail) + ")"
        elif s.status == "priority":
            word = "faculty/staff priority"
        elif s.status == "unavailable":
            word = s.detail.lower() or "unavailable"
        else:
            word = s.status
        groups.setdefault(word, []).append(s.court)
    return " · ".join(f"court{'s' if len(cs) > 1 else ''} {' '.join(sorted(cs, key=court_key))} {word}"
                      for word, cs in groups.items())


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
        raise UTCourtsError("Say which sport or facility.", hint=BOOK_EXAMPLE)
    if at is None:
        raise UTCourtsError("Say what time.", hint="For example 9pm, 9:30pm or 21:00.")
    day = day or today
    if day < today or day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise UTCourtsError(f"RecSports only books today through {fmt_day(today + timedelta(days=MAX_DAYS_AHEAD))}.")
    facilities = resolve_facilities(fac_terms)
    interactive = sys.stdin.isatty()
    allow_login = args.login and interactive
    want = hhmm(at)
    p = Paint(sys.stdout)
    when = f"{fmt_time(at)} on {fmt_day(day)}"

    with Spinner(_checking(facilities), enabled=not args.json):
        grids = get_results([(day, f.id) for f in facilities], allow_login)
    court_ok = (lambda s: s.court.lower() == args.court.lower()) if args.court else (lambda s: True)
    cands = [(g, s) for g in grids for s in sorted(g.slots, key=lambda s: court_key(s.court))
             if s.time == want and s.status == "open" and court_ok(s)]
    base = {"date": day.isoformat(), "time": want}

    if not cands:
        lines, near_all = [p.bad(f"Court {args.court} isn't open at {when}" if args.court
                                 else f"Nothing open at {when}")], []
        lw = len("closest open")
        at_label = f"at {fmt_time(at)}".ljust(lw)
        has_court = [g for g in grids if args.court and any(c.lower() == args.court.lower() for c in g.courts)]
        for g in grids:
            here = [s for s in g.slots if s.time == want and court_ok(s)]
            what = ", ".join(f"{s.court}: {s.detail or s.status}" for s in here) or "no slot at that time"
            opens = sorted(g.open_by_time(), key=lambda x: abs(_mins(x["time"]) - at))[:3]
            near_all.append({"facility": g.facility, "at_that_time": what, "closest_open": opens})
            if has_court and g not in has_court:
                continue  # no court by that name here; just noise
            lines += ["", "  " + fac_title(p, g.facility_id, g.facility),
                      f"    {p(at_label, 'dim')}  "
                      + (_taken_words(here) if here else p(
                          f"no court {args.court} here" if args.court and g not in has_court
                          else "no slot at that time", "dim"))]
            opens = sorted(opens, key=lambda x: x["time"])
            tw = max((len(fmt_time(_mins(x["time"]))) for x in opens), default=0)
            for i, x in enumerate(opens):
                lines.append(f"    {p(('closest open' if i == 0 else '').ljust(lw), 'dim')}  "
                             f"{fmt_time(_mins(x['time'])):>{tw}}  {_courts_line(p, x['courts'])}")
            if not opens:
                lines.append(f"    {p('nothing else open this day', 'dim')}")
        _emit(args, {**base, "status": "not_open", "facilities": near_all}, "\n".join(lines))
        return NOTHING_DONE

    if len(cands) > 1:
        options = [{"facility": g.facility, "facility_id": g.facility_id, "court": s.court, "label": s.label}
                   for g, s in cands]
        cw = max(len(s.court) for _, s in cands)
        by_fac = [list(grp) for _, grp in groupby(cands, key=lambda gs: gs[0].facility_id)]
        if interactive and not args.json and not args.yes and not args.dry_run:
            nw = len(str(len(cands)))
            lines, i = [p(f"{len(cands)} courts are open at {when}", "bold")], 0
            for grp in by_fac:
                lines += ["", "  " + fac_title(p, grp[0][0].facility_id, grp[0][0].facility)]
                for _, s in grp:
                    i += 1
                    lines.append(f"    {p(str(i).rjust(nw), 'dim')}  court {s.court}")
            print("\n".join(lines) + "\n")
            pick = _ask(f"Which one? [1-{len(cands)}, Enter to stop] ")
            if not pick.isdigit() or not 1 <= int(pick) <= len(cands):
                print(p("Nothing booked.", "dim"))
                return NOTHING_DONE
            cands = [cands[int(pick) - 1]]
        else:
            lines = [p.warn(f"{len(cands)} courts are open at {when}. Pick one:")]
            for grp in by_fac:
                lines += ["", "  " + fac_title(p, grp[0][0].facility_id, grp[0][0].facility)]
                for g, s in grp:
                    cmd = (f"utcourts book {fac_alias(g.facility_id) or g.facility_id} {day_token(day)} "
                           f"{time_token(want)} --court {s.court}")
                    lines.append(f"    court {s.court:<{cw}}  {p(cmd, 'dim')}")
            _emit(args, {**base, "status": "ambiguous", "options": options}, "\n".join(lines))
            return NOTHING_DONE

    g, s = cands[0]
    what = slot_desc(g.facility_id, g.facility, g.date, s.time, s.court)
    slot = {**base, "facility": g.facility, "facility_id": g.facility_id, "court": s.court, "label": s.label}
    if args.dry_run:
        _emit(args, {**slot, "status": "dry_run"}, p.note(f"Would book {what}") + p("  (dry run)", "dim"))
        return 0
    if not args.yes:
        if not interactive:
            _emit(args, {**slot, "status": "not_confirmed"},
                  p.warn(f"Not booked: {what}") + "\n  There's no terminal to confirm in. Add --yes to book it.")
            return NOTHING_DONE
        if _ask(f"Book {what}? [y/N] ").lower() not in ("y", "yes"):
            print(p("Nothing booked.", "dim"))
            return NOTHING_DONE

    with Spinner("Booking…", enabled=not args.json):
        out = with_client(make_book_action(day, g.facility_id, s.time, s.court), allow_login)
    msgs = out.get("messages", [])
    lines = {
        "booked": [p.ok(f"Booked {what}"), *_said(p, msgs, quiet=True),
                   p("  Confirmed on the grid. To cancel: utcourts cancel", "dim")],
        "failed": [p.bad(f"Not booked: {what}"), *(_said(p, msgs) or ["  The court still shows as open."])],
        "unclear": [p.warn(f"Not sure it went through: {what}"), *_said(p, msgs), "  Check with: utcourts mine"],
        "taken": [p.bad(f"Someone else just took it: {what}")],
    }[out["status"]]
    _emit(args, {**slot, **out}, "\n".join(lines))
    return 0 if out["status"] == "booked" else NOTHING_DONE


_WEEKDAY_WORD = re.compile(r"\b(Mon(day)?|Tue(s|sday)?|Wed(nesday)?|Thu(r|rs|rsday)?|Fri(day)?|Sat(urday)?|"
                           r"Sun(day)?)\b\.?,?", re.I)


def reservation_parts(text: str) -> tuple[str, str, str] | None:
    """'WC - PB - 11 11/18/2026 8:00 PM' -> ('Wed Nov 18', '8 PM', 'WC - PB - 11'); None if no date + time."""
    md = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    mt = re.search(r"\b(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\b\.?", text)
    if not md or not mt:
        return None
    try:
        year = int(md[3]) + (2000 if len(md[3]) == 2 else 0)
        d = date(year, int(md[1]), int(md[2]))
    except ValueError:
        return None
    mins = (int(mt[1]) % 12 + (12 if mt[3].lower() == "p" else 0)) * 60 + int(mt[2])
    rest = text
    for a, b in sorted([md.span(), mt.span()], reverse=True):
        rest = rest[:a] + " " + rest[b:]
    rest = re.sub(r"\s+", " ", _WEEKDAY_WORD.sub(" ", rest)).strip(" -,|·")
    return fmt_day(d), fmt_time(mins), rest


def reservation_desc(text: str) -> str:
    parts = reservation_parts(text)
    return f"{parts[2]} · {parts[0]} · {parts[1]}" if parts and parts[2] else text


def _reservation_lines(p: Paint, items: list[Reservation]) -> list[str]:
    rows = [(r, reservation_parts(r.text)) for r in items]
    dw = max((len(x[0]) for _, x in rows if x), default=0)
    tw = max((len(x[1]) for _, x in rows if x), default=0)
    nw = len(str(len(items)))
    lines = []
    for r, x in rows:
        body = f"{x[0]:<{dw}}  {x[1]:>{tw}}  {x[2]}" if x else r.text
        lines.append(f"  {p(str(r.index).rjust(nw), 'dim')}  {body}"
                     + ("" if r.cancel_method else p("  (cancel on the website)", "dim")))
    return lines


def cmd_mine(args) -> int:
    with Spinner("Loading your reservations…", enabled=not args.json):
        items, notices, _ = with_client(fetch_reservations, args.login and sys.stdin.isatty())
    payload = {"source": MINE_URL, "notices": notices,
               "reservations": [{"index": r.index, "text": r.text, "can_cancel": r.cancel_method is not None}
                                for r in items]}
    p = Paint(sys.stdout)
    if items:
        lines = [p("Your reservations", "bold"), "", *_reservation_lines(p, items)]
        if any(r.cancel_method for r in items):
            lines += ["", p("To cancel one: utcourts cancel <number>", "dim")]
    else:
        lines = ["No reservations."]
    if notices:
        lines += [""] + [p(n, "dim") for n in notices]
    _emit(args, payload, "\n".join(lines))
    return 0


def _pick_reservation(items: list[Reservation], which: str | None, match: str | None) -> Reservation | None:
    if which:
        if not which.isdigit() or not 1 <= int(which) <= len(items):
            raise UTCourtsError(f"There's no reservation #{which}.", hint="See the numbers: utcourts mine")
        return items[int(which) - 1]
    if match:
        words = match.lower().split()
        hits = [r for r in items if all(w in r.text.lower() for w in words)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise UTCourtsError(f"No reservation matches “{match}”.", hint="See them all: utcourts mine")
        raise UTCourtsError(f"{len(hits)} reservations match “{match}”.", hint="Be more specific, or use the number.")
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
    p = Paint(sys.stdout)
    with Spinner("Loading your reservations…", enabled=not args.json):
        items, notices, _ = with_client(fetch_reservations, allow_login)
    if not items:
        _emit(args, {"status": "not_found", "notices": notices}, "No reservations to cancel.")
        return NOTHING_DONE
    target = _pick_reservation(items, args.which, args.match)
    if target is None:
        if interactive and not args.json and not args.yes:
            print("\n".join([p("Your reservations", "bold"), "", *_reservation_lines(p, items), ""]))
            pick = _ask(f"Which one? [1-{len(items)}, Enter to stop] ")
            if not pick:
                print(p("Nothing cancelled.", "dim"))
                return NOTHING_DONE
            target = _pick_reservation(items, pick, None)
        else:
            _emit(args, {"status": "ambiguous", "reservations": [{"index": r.index, "text": r.text} for r in items]},
                  "\n".join([p.warn("Which one? Run utcourts cancel <number>"), "", *_reservation_lines(p, items)]))
            return NOTHING_DONE
    base = {"reservation": target.text, "index": target.index}
    what = reservation_desc(target.text)
    if target.cancel_method is None:
        _emit(args, {**base, "status": "unsupported"},
              p.bad(f"Can't cancel this one from here: {what}")
              + f"\n  Its cancel button only works on the website: {MINE_URL}")
        return NOTHING_DONE
    if args.dry_run:
        _emit(args, {**base, "status": "dry_run"}, p.note(f"Would cancel {what}") + p("  (dry run)", "dim"))
        return 0
    if not args.yes:
        if not interactive:
            _emit(args, {**base, "status": "not_confirmed"},
                  p.warn(f"Not cancelled: {what}") + "\n  There's no terminal to confirm in. Add --yes to cancel it.")
            return NOTHING_DONE
        if _ask(f"Cancel {what}? [y/N] ").lower() not in ("y", "yes"):
            print(p("Nothing cancelled.", "dim"))
            return NOTHING_DONE
    with Spinner("Cancelling…", enabled=not args.json):
        out = with_client(make_cancel_action(target.text), allow_login)
    msgs = out.get("messages", [])
    lines = {
        "cancelled": [p.ok(f"Cancelled {what}"), *_said(p, msgs, quiet=True),
                      p("  Confirmed: it's gone from My Reservations.", "dim")],
        "failed": [p.bad(f"Not cancelled: {what}"), *(_said(p, msgs) or ["  It still shows on My Reservations."])],
        "unclear": [p.warn(f"Not sure it went through: {what}"), *_said(p, msgs), "  Check with: utcourts mine"],
        "not_found": [p.bad(f"It's no longer on My Reservations: {what}")],
    }[out["status"]]
    _emit(args, {**base, **out}, "\n".join(lines))
    return 0 if out["status"] == "cancelled" else NOTHING_DONE


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
COMMANDS = ("check", "book", "reserve", "mine", "reservations", "cancel", "login", "status",
            "logout", "facilities", "help")


def print_top_help() -> None:
    p = Paint(sys.stdout)
    h = lambda s: p(s, "bold")  # noqa: E731
    commands = [("check", "open courts (the default)"),
                ("book", "book a court, after you confirm"),
                ("mine", "your reservations"),
                ("cancel", "cancel a reservation, after you confirm"),
                ("login", "sign in with your UT EID + Duo"),
                ("status", "is the saved sign-in still good?"),
                ("facilities", "facility short names"),
                ("logout", "forget the saved sign-in")]
    print("\n".join([
        f"{h('utcourts')} {p(__version__, 'dim')}  UT RecSports courts from the terminal",
        "",
        h("Usage"),
        "  utcourts [check] [sport|facility] [day] [--after 6pm] [--before 9pm]",
        "  utcourts <command> [options]",
        "",
        h("Commands"),
        *[f"  {name:<12}{what}" for name, what in commands],
        "",
        h("Examples"),
        "  utcourts squash tomorrow --after 6pm",
        "  utcourts check rb fri --grid",
        "  utcourts book gre-squash tomorrow 9pm --court B",
        "  utcourts cancel 1",
        "",
        h("Words"),
        "  sports  squash · racquetball (rb) · handball · pickleball (pb) · tennis",
        "  days    today · tomorrow · fri · +2 · 11/20   " + p(f"up to {MAX_DAYS_AHEAD} days ahead", "dim"),
        "  times   9pm · 9:30pm · 21:00",
        "",
        p("More on one command: utcourts help <command>", "dim"),
    ]))


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str):
        super().__init__(prog, max_help_position=24, width=min(shutil.get_terminal_size((88, 24)).columns, 88))

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix=Paint(sys.stdout)("Usage:", "bold") + " ")

    def start_section(self, heading):
        return super().start_section(Paint(sys.stdout)(heading, "bold") if heading else heading)


class _Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        if sys.version_info >= (3, 14):
            kw.setdefault("color", False)  # we do our own
        kw.setdefault("add_help", False)
        kw.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*a, **kw)
        self._optionals.title = "Options"
        self.add_argument("-h", "--help", action="help", help=argparse.SUPPRESS)

    def error(self, message: str):
        e = Paint(sys.stderr)
        words = self.prog.split()
        cmd = words[1] if len(words) > 1 else getattr(self, "command", None)
        log(e.bad(message[:1].upper() + message[1:]))
        log(f"  See: utcourts help{' ' + cmd if cmd else ''}")
        raise SystemExit(1)


def _examples(*rows: tuple[str, str]) -> str:
    p = Paint(sys.stdout)
    w = max(len(cmd) for cmd, _ in rows)
    return p("Examples:", "bold") + "\n" + "\n".join(
        f"  {cmd:<{w}}  {p(note, 'dim')}".rstrip() for cmd, note in rows)


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    p = _Parser(prog="utcourts")
    sub = p.add_subparsers(dest="cmd")
    subs: dict[str, argparse.ArgumentParser] = {}

    def common(sp, json_help: bool = True):
        sp.add_argument("--json", action="store_true", help="JSON output" if json_help else argparse.SUPPRESS)
        sp.add_argument("--no-login", dest="login", action="store_false",
                        help="never open a sign-in window" if json_help else argparse.SUPPRESS)

    c = sub.add_parser("check", usage="utcourts [check] [sport|facility …] [day] [options]",
                       description="Show open courts. Sports, facility short names and the day can go in any order.",
                       epilog=_examples(("utcourts squash tomorrow --after 6pm", ""),
                                        ("utcourts check rb gre-squash fri --before 9pm", ""),
                                        ("utcourts check tennis --days 3", ""),
                                        ("utcourts check gre-rb +2 --grid", "everything, not just what's open")))
    c.add_argument("terms", nargs="*", help=argparse.SUPPRESS)
    c.add_argument("-s", "--sport", action="append", help=argparse.SUPPRESS)
    c.add_argument("-d", "--date", help=argparse.SUPPRESS)
    c.add_argument("--after", metavar="TIME", help="only times from TIME on (5pm, 17:30)")
    c.add_argument("--before", metavar="TIME", help="only times before TIME")
    c.add_argument("--days", type=int, default=1, metavar="N", help="N days, starting from the day")
    c.add_argument("--grid", action="store_true", help="the whole grid, not just open courts")
    c.add_argument("--all", action="store_true", help="--grid; with --json, every slot")
    common(c)
    c.set_defaults(func=cmd_check)
    subs["check"] = c

    b = sub.add_parser("book", aliases=["reserve"],
                       usage="utcourts book <sport|facility> <day> <time> [--court NAME] [options]",
                       description="Book one open court. It shows exactly what it will book and asks first,\n"
                                   "then re-checks the grid to make sure it worked.",
                       epilog=_examples(("utcourts book gre-squash tomorrow 9pm --court B", ""),
                                        ("utcourts book squash fri 6pm", "pick from a list"),
                                        ("utcourts book rsc-rb +2 18:00 -n", "preview only")))
    b.add_argument("terms", nargs="*", help=argparse.SUPPRESS)
    b.add_argument("--court", metavar="NAME", help="court as named on the grid (1, A, B1A…)")
    b.add_argument("--at", help=argparse.SUPPRESS)
    b.add_argument("-d", "--date", help=argparse.SUPPRESS)
    b.add_argument("-n", "--dry-run", action="store_true", help="show what would be booked, then stop")
    b.add_argument("-y", "--yes", action="store_true", help="book without asking")
    common(b)
    b.set_defaults(func=cmd_book)
    subs["book"] = subs["reserve"] = b

    m = sub.add_parser("mine", aliases=["reservations"], usage="utcourts mine [options]",
                       description="Your reservations from My RecSports, numbered for `utcourts cancel`.")
    common(m)
    m.set_defaults(func=cmd_mine)
    subs["mine"] = subs["reservations"] = m

    x = sub.add_parser("cancel", usage="utcourts cancel [number] [--match WORDS] [options]",
                       description="Cancel a reservation, picked by its number from `utcourts mine` or by words\n"
                                   "in it. It asks first, then checks that it's gone.",
                       epilog=_examples(("utcourts cancel 1", ""),
                                        ('utcourts cancel --match "11/18 9:00 PM"', ""),
                                        ("utcourts cancel", "pick from a list")))
    x.add_argument("which", nargs="?", help=argparse.SUPPRESS)
    x.add_argument("--match", metavar="WORDS", help="pick the reservation that contains these words")
    x.add_argument("-n", "--dry-run", action="store_true", help="show what would be cancelled, then stop")
    x.add_argument("-y", "--yes", action="store_true", help="cancel without asking")
    common(x)
    x.set_defaults(func=cmd_cancel)
    subs["cancel"] = x

    lg = sub.add_parser("login", usage="utcourts login [--no-save-password]",
                        description="Sign in with your UT EID + Duo in a separate Chrome window. After that,\n"
                                    "utcourts signs itself back in quietly until UT asks for Duo again.\n\n"
                                    "The EID and password you type in the window are kept in the system\n"
                                    "keychain. When UT asks for them again, utcourts types them in without a\n"
                                    "window, and you approve the Duo push on your phone (if Duo still trusts\n"
                                    "the browser, not even that). `utcourts logout` forgets them.")
    lg.add_argument("--no-save-password", dest="save_password", action="store_false",
                    help="don't keep your password (and forget a saved one); sticks until --save-password")
    lg.add_argument("--save-password", dest="save_password", action="store_true",
                    help="turn saving back on")
    lg.set_defaults(func=cmd_login, save_password=None)
    subs["login"] = lg

    st = sub.add_parser("status", usage="utcourts status [--refresh] [-q]",
                        description="Check whether the saved sign-in still works.")
    st.add_argument("--refresh", action="store_true", help="if it expired, try a quiet sign-in")
    st.add_argument("-q", "--quiet", action="store_true", help="print nothing when signed in")
    common(st, json_help=False)
    st.set_defaults(func=cmd_status)
    subs["status"] = st

    fa = sub.add_parser("facilities", usage="utcourts facilities",
                        description="Facilities, the sports they have, and their short names.")
    common(fa, json_help=False)
    fa.set_defaults(func=cmd_facilities)
    subs["facilities"] = fa

    lo = sub.add_parser("logout", usage="utcourts logout",
                        description="Delete the saved session and the private browser profile.")
    lo.set_defaults(func=cmd_logout)
    subs["logout"] = lo

    h = sub.add_parser("help", usage="utcourts help [command]")
    h.add_argument("topic", nargs="?")
    h.set_defaults(func=None)
    subs["help"] = h
    return p, subs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("-h", "--help"):
        print_top_help()
        return 0
    if argv and argv[0] in ("-V", "--version"):
        print(f"utcourts {__version__}")
        return 0
    if not argv or argv[0] not in COMMANDS:
        argv = ["check", *argv]  # `utcourts squash tomorrow` == `utcourts check squash tomorrow`
    parser, subs = build_parser()
    parser.command = argv[0]  # so a stray option is reported against its command
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # --help, or a usage error (already printed)
        return e.code if isinstance(e.code, int) else 0
    if args.cmd == "help":
        sp = subs.get(args.topic or "")
        sp.print_help() if sp else print_top_help()
        return 0
    try:
        return args.func(args)
    except UTCourtsError as e:
        p = Paint(sys.stderr)
        log(p.bad(str(e)))
        for line in (e.hint or "").splitlines():
            log(f"  {line}")
        return e.exit_code
    except KeyboardInterrupt:
        log("")
        return 130


if __name__ == "__main__":
    sys.exit(main())
