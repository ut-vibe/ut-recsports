"""A fake apps.rs.utexas.edu + fake UT SSO, built to match the real page's markup.

App:  http://127.0.0.1:<app_port>/app/myrecsports/reserve_courts.php
SSO:  http://localhost:<sso_port>/idp/sso   (a different host, like enterprise.login.utexas.edu)

Knobs (module globals, flipped by the tests):
  APP_SESSIONS   valid app session ids
  SSO_ALIVE      if True, the SSO auto-signs you back in (no password)
"""
from __future__ import annotations

import html
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

AUSTIN = ZoneInfo("America/Chicago")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs
import secrets

APP_SESSIONS: set[str] = set()
SSO_ALIVE = True
RAW_MARKUP = True  # False = tidier markup variant (tbody, escaped titles)
HUMAN_AUTOSUBMIT = False  # login page "types" by itself after a moment (for the visible-window test)
APP_PORT = SSO_PORT = 0
REQUEST_LOG: list[str] = []
SLOT_INDEX: dict[int, tuple] = {}   # slot id -> (date, fac, label, court)
BOOKED: dict[tuple, int] = {}       # (date, fac, label, court) -> booking id
BOOK_REQUESTS: list[int] = []       # every reserve request that reached the app
REFUSE_BOOKING = None               # None | "alert-box" | "js": the site says no
CANCEL_STYLE = "link"               # "link" (GET) or "form" (POST)

# Grids captured from the live site (Wed 11/18/2026). O open, R reserved, P priority,
# E event, F facility closed, ? reserved with empty tooltip.
GRIDS = {
    35: (["1", "3", "4", "5", "6", "7"], "RB", "GRE", "Handball (UTSCA)", [
        ("12:00 PM", "PPPRPP"), ("1:00 PM", "PPPPPP"), ("2:00 PM", "OOOOOO"),
        ("3:00 PM", "OOOOOO"), ("4:00 PM", "OOROOO"), ("5:00 PM", "EEEEER"),
        ("6:00 PM", "EEEEER"), ("7:00 PM", "EEEEEO"), ("8:00 PM", "EEEEER"),
        ("9:00 PM", "OROOOR"), ("10:00 PM", "OROOOO"), ("11:00 PM", "FFFFFF")]),
    65: (["1", "2", "3", "4", "5", "11", "12", "13", "14", "15", "B1A", "B1B", "B2A", "B2B", "B3A", "B3B", "B4A", "B4B"],
         "PB", "WC", "Private Tennis Lessons (INS)", [
        ("4:00 PM", "EEEEEROOOOOOOOOROO"), ("5:00 PM", "EEEEERROOOOOOOOOOO"),
        ("6:00 PM", "EEEEEORORORRRRROOO"), ("7:00 PM", "EEEEERRRRRRRRRRRRR"),
        ("8:00 PM", "EEEEEOR?RROROOORRR"), ("9:00 PM", "EEEEEOROOORRROOOOO")]),
    40: (["A", "B"], "SQ", "GRE", "Squash Club", [
        ("12:00 PM", "OR"), ("5:00 PM", "RR"), ("6:00 PM", "OO"), ("9:00 PM", "RO")]),
    28: (["932"], "SQ", "BEL", "x", [("6:00 PM", "O"), ("7:00 PM", "R")]),
    55: (["10", "9"], "SQ", "RSC", "x", [("6:00 PM", "RR"), ("7:00 PM", "RO")]),
}
OPTIONS = [(28, "Bellmont Hall (BEL) - Squash"), (30, "Caven-Clark Courts (CCF) - Pickleball Only"),
           (35, "Gregory Gym (GRE) - Racquetball / Handball"), (40, "Gregory Gym (GRE) - Squash"),
           (55, "Recreational Sports Center (RSC) - Squash"),
           (50, "Recreational Sports Center (RSC) - Racquetball / Handball"),
           (60, "Whitaker Courts (WC) - Tennis Only"), (65, "Whitaker Courts (WC) - Pickleball / Tennis")]


def cell(kind: str, label: str, court: str, code: str, ct: str, d: date, event: str, sid: int) -> str:
    if kind == "O":
        desc = f"Reserve court {code} - {d:%a} {d.month}/{d.day}, {label} - Court {court.zfill(2)}"
        amp = "&" if RAW_MARKUP else "&amp;"
        return (f'<td class="success text-center text-success cursorHover p-1">\n\t\t\t\t\t\t\t'
                f'<a href="reserve_courts.php?id={sid}{amp}reservationAction=reserve{amp}courtType={ct}" '
                f'onclick="return confirm(\'{desc} \')"><i class="fas fa-plus-circle text-success" '
                f'aria-label="{desc}" title="{desc}"></i></a>\n\t\t\t\t\t\t</td>\n\t\t\t\t\t\t')
    title, icon = {
        "R": ("Reserved", "fa fa-user"),
        "?": ("", "fa fa-user"),
        "P": (("<strong>Unavailable - Reason:</strong> Faculty/Staff Priority Hours" if RAW_MARKUP else
               html.escape("<strong>Unavailable - Reason:</strong> Faculty/Staff Priority Hours")), "fa fa-clock-o"),
        "E": (f"Event Conflict: {event}", "fa fa-user"),
        "F": ("Facility Closed", "fad fa-clock"),
    }[kind]
    aria = (' aria-label="Clock Icon indicating that this reservable time is restricted to Faculty/Staff"'
            if kind == "P" else "")
    return (f'<td class="danger text-center text-rs-red-strong p-1" data-toggle="tooltip" '
            f'title="{title}">\n\t\t\t\t\t\t\t<i class="{icon}"{aria}></i>\n\t\t\t\t\t\t</td>\n\t\t\t\t\t\t')


def page(d: date, fac: int, today: date, message: str = "") -> str:
    courts, ct, code, event, rows = GRIDS.get(fac, (["1"], "XX", "ZZZ", "x", [("6:00 PM", "O")]))
    opts = "".join(f'<option value="{v}">{html.escape(n)}</option>' for v, n in OPTIONS)
    if d == today and fac == 35:
        rows = rows[-2:]  # "late in the day" — only the last hours remain
    if d == today + timedelta(days=7):
        rows = []  # pretend nothing is bookable on the last day
    table = ""
    if rows:
        head = '<th class="text-middle text-center">Time</th>' + "".join(
            f'<th class="text-center text-middle p-0">{c}</th>' for c in courts)
        body = ""
        sid = int(d.strftime("%m%d")) * 100000 + fac * 100
        tds_rows = []
        for label, kinds in rows:
            cells = []
            for k, c in zip(kinds, courts):
                sid += 1
                if (d, fac, label, c) in BOOKED:
                    k = "R"
                if k == "O":
                    SLOT_INDEX[sid] = (d, fac, label, c)
                cells.append(cell(k, label, c, code, ct, d, event, sid))
            tds_rows.append((label, "".join(cells)))
        for label, tds in tds_rows:
            body += f'<tr><td class="text-center p-0 text-middle" nowrap><strong>{label}</strong></td>\n\t\t\t\t\t\t{tds}</tr>'
        if RAW_MARKUP:  # exactly like the live site: no <tbody>, header <tr> never closed
            table = (f'<div class="card table-responsive"><table class="table table-bordered table-striped '
                     f'table-hover p-0 mb-0">\n\t\t<thead class="bg-rs-yellow text-black">\n\t\t\t<tr>\n'
                     f'\t\t\t\t{head}</thead>{body}</table></div>')
        else:
            table = (f'<div class="card table-responsive"><table class="table table-bordered table-striped '
                     f'table-hover p-0 mb-0"> <thead class="bg-rs-yellow text-black"> <tr> {head}</tr></thead>'
                     f'<tbody>{body}</tbody></table></div>')
    return f"""<!DOCTYPE html><html><head><title>My UT RecSports</title></head><body>
<nav><a href="/logout">Logout</a></nav>
<div class="container-fluid">
<h3>Court Reservations</h3>
{message}
<form id="form_courts" method="get">
  <input type="text" name="date" id="date" class="form_change" value="{d:%m/%d/%Y} ({d:%a})">
  <select name="facility_id" id="facility_id" class="form_change">{opts}</select>
</form>
{table}
</div>
<script>
\t$(document).ready(function()
\t{{
\t\t$("#facility_id").val("{fac}");
\t}});
</script>
</body></html>"""


FAC_NAME = dict(OPTIONS)


def mine_page(message: str = "") -> str:
    rows = ""
    for (d, fac, label, court), bid in sorted(BOOKED.items(), key=lambda kv: kv[1]):
        code = FAC_NAME[fac].split("(")[1].split(")")[0]
        kind = FAC_NAME[fac].split(" - ")[-1]
        if CANCEL_STYLE == "link":
            ctl = (f'<a href="myreservations.php?cancel_id={bid}&action=cancel" class="btn btn-danger btn-sm" '
                   f'onclick="return confirm(\'Cancel this reservation?\')">Cancel</a>')
        else:
            ctl = (f'<form method="post" action="myreservations.php"><input type="hidden" name="reservation_id" '
                   f'value="{bid}"><button type="submit" name="action" value="cancel" '
                   f'class="btn btn-danger btn-sm">Cancel</button></form>')
        rows += (f"<tr><td>{code} - {kind}</td><td>Court {court}</td><td>{d:%a %m/%d/%Y}</td>"
                 f"<td>{label}</td><td>{ctl}</td></tr>")
    table = (f'<table class="table"><tr><th>Facility</th><th>Court</th><th>Date</th><th>Time</th><th></th></tr>'
             f'{rows}</table>') if rows else ""
    return f"""<html><body><nav><a href="/logout">Logout</a> <a href="profile.php">Settings</a></nav>
<div class="container-fluid"><h3>My Reservations</h3>{message}
<div class="card"><h5 class="card-header">Court Reservations</h5><div class="card-body">{table}</div></div>
<div class="card"><h5 class="card-header">TeXercise</h5><div class="card-body">
<p>You have no upcoming TeXercise reservations.</p></div></div></div></body></html>"""


def login_page() -> str:
    robot = ("<script>setTimeout(() => document.forms[0].submit(), 1500)</script>" if HUMAN_AUTOSUBMIT else "")
    return f"""<html><body><h1>UT EID Login</h1>
<form method="post" action="/idp/login"><input name="j_username"><input type="password" name="j_password">
<button>Sign in</button></form>{robot}</body></html>"""


def autopost_back(target: str) -> str:
    # what a SAML IdP returns: a self-submitting form that POSTs back to the app
    return f"""<html><body onload="document.forms[0].submit()">
<form method="post" action="{target}"><input type="hidden" name="SAMLResponse" value="xyz"></form>
<noscript><button>Continue</button></noscript></body></html>"""


class App(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _cookies(self) -> SimpleCookie:
        c = SimpleCookie()
        c.load(self.headers.get("Cookie", ""))
        return c

    def _send(self, code: int, body: str = "", headers: dict | None = None):
        b = body.encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            if k == "Set-Cookie" and isinstance(v, list):
                for one in v:
                    self.send_header(k, one)
            else:
                self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        REQUEST_LOG.append(f"APP GET {u.path} ua={self.headers.get('User-Agent','')[:40]}")
        if u.path == "/app/myrecsports/reserve_courts.php":
            sid = self._cookies().get("PHPSESSID")
            if not sid or sid.value not in APP_SESSIONS:
                return self._send(302, headers={"Location": f"http://localhost:{SSO_PORT}/idp/sso"})
            today = datetime.now(AUSTIN).date()
            if q.get("reservationAction", [""])[0] == "reserve":
                sid_ = int(q.get("id", ["0"])[0])
                BOOK_REQUESTS.append(sid_)
                d, fac, label, court = SLOT_INDEX[sid_]
                if REFUSE_BOOKING == "alert-box":
                    msg = '<div class="alert alert-danger">You have reached the maximum number of reservations for this day.</div>'
                elif REFUSE_BOOKING == "js":
                    msg = "<script>alert('You may only make one reservation per day.');</script>"
                elif (d, fac, label, court) in BOOKED:
                    msg = '<div class="alert alert-danger">That court is no longer available.</div>'
                else:
                    BOOKED[(d, fac, label, court)] = len(BOOK_REQUESTS) + 500
                    msg = '<div class="alert alert-success">Your reservation has been confirmed.</div>'
                return self._send(200, page(d, fac, today, msg))
            try:
                d = datetime.strptime(q.get("date", [""])[0][:10], "%m/%d/%Y").date()
            except ValueError:
                d = today + timedelta(days=7)  # the real site does this for garbage
            d = min(max(d, today), today + timedelta(days=7))  # clamp like the real site
            fac = int(q.get("facility_id", ["35"])[0])
            return self._send(200, page(d, fac, today))
        if u.path == "/app/myrecsports/myreservations.php":
            sid = self._cookies().get("PHPSESSID")
            if not sid or sid.value not in APP_SESSIONS:
                return self._send(302, headers={"Location": f"http://localhost:{SSO_PORT}/idp/sso"})
            if q.get("action", [""])[0] == "cancel":
                return self._send(200, mine_page(self._cancel(int(q.get("cancel_id", ["0"])[0]))))
            return self._send(200, mine_page())
        self._send(404, "nope")

    def _cancel(self, bid: int) -> str:
        for k, v in list(BOOKED.items()):
            if v == bid:
                del BOOKED[k]
                return '<div class="alert alert-success">Your reservation has been cancelled.</div>'
        return '<div class="alert alert-danger">Reservation not found.</div>'

    def do_POST(self):
        u = urlparse(self.path)
        REQUEST_LOG.append(f"APP POST {u.path}")
        if u.path == "/app/myrecsports/myreservations.php":
            body = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            sid = self._cookies().get("PHPSESSID")
            if not sid or sid.value not in APP_SESSIONS:
                return self._send(302, headers={"Location": f"http://localhost:{SSO_PORT}/idp/sso"})
            if body.get("action", [""])[0] == "cancel":
                return self._send(200, mine_page(self._cancel(int(body.get("reservation_id", ["0"])[0]))))
            return self._send(200, mine_page())
        if u.path == "/Shibboleth.sso/SAML2/POST":
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            sid = secrets.token_hex(8)
            APP_SESSIONS.add(sid)
            return self._send(302, headers={"Location": "/app/myrecsports/",
                                            "Set-Cookie": f"PHPSESSID={sid}; path=/; HttpOnly"})
        self._send(404)


class AppHome(App):
    def do_GET(self):
        if urlparse(self.path).path == "/app/myrecsports/":
            REQUEST_LOG.append("APP GET /app/myrecsports/ (home)")
            return self._send(200, "<html><body>My RecSports home</body></html>")
        return super().do_GET()


class SSO(App):
    def do_GET(self):
        REQUEST_LOG.append(f"SSO GET {self.path}")
        if SSO_ALIVE and self._cookies().get("idp_session"):
            return self._send(200, autopost_back(f"http://127.0.0.1:{APP_PORT}/Shibboleth.sso/SAML2/POST"))
        return self._send(200, login_page())

    def do_POST(self):
        REQUEST_LOG.append(f"SSO POST {self.path}")
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        return self._send(200, autopost_back(f"http://127.0.0.1:{APP_PORT}/Shibboleth.sso/SAML2/POST"),
                          headers={"Set-Cookie": "idp_session=1; path=/; HttpOnly"})


def start() -> tuple[int, int]:
    global APP_PORT, SSO_PORT
    app = ThreadingHTTPServer(("127.0.0.1", 0), AppHome)
    sso = ThreadingHTTPServer(("127.0.0.1", 0), SSO)
    APP_PORT, SSO_PORT = app.server_address[1], sso.server_address[1]
    for s in (app, sso):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return APP_PORT, SSO_PORT
