"""End-to-end-ish tests for utcourts against the mock site. Run: python3 tests/run_tests.py"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_site  # noqa: E402

app_port, sso_port = mock_site.start()
tmp_home = tempfile.mkdtemp(prefix="utcourts-test-")
os.environ["UTCOURTS_BASE_URL"] = f"http://127.0.0.1:{app_port}"
os.environ["UTCOURTS_HOME"] = tmp_home
os.environ["UTCOURTS_BROWSER"] = "chromium"
os.environ["NO_COLOR"] = "1"
sys.path.insert(0, str(HERE.parent / "plugins/ut-recsports/skills/courts/scripts"))
import utcourts as U  # noqa: E402

PASS = 0


def ok(cond, msg):
    global PASS
    if not cond:
        raise AssertionError(msg)
    PASS += 1
    print(f"  ok  {msg}")


def run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = U.main(list(argv))
    return code, out.getvalue(), err.getvalue()


today = U.austin_today()
wed = date(2026, 11, 18)

print("parser")
r = U.parse_courts_page(mock_site.page(wed, 35, today=date(2026, 11, 17)))
ok(r.facility_id == 35 and r.date == "2026-11-18" and r.weekday == "Wed", "reads facility + date from the page")
ok(r.courts == ["1", "3", "4", "5", "6", "7"], "court names from the header")
grid = {}
for s in r.slots:
    grid.setdefault(s.label, "")
    grid[s.label] += {"open": "O", "reserved": "R", "priority": "P", "event": "E", "closed": "F"}[s.status]
expected = dict(mock_site.GRIDS[35][4])
ok(grid == expected, "every cell's status matches the live grid")
pri = next(s for s in r.slots if s.status == "priority")
ok(pri.detail == "Faculty/Staff Priority Hours", "priority-hours reason has the HTML stripped")
ev = next(s for s in r.slots if s.status == "event")
ok(ev.detail == "Event Conflict: Handball (UTSCA)", "event name kept")
op = next(s for s in r.slots if s.status == "open")
ok(op.slot_id and op.slot_id.isdigit() and op.time == "14:00", "open slot has an id and 24h time")
ok(r.open_by_time(after=U.parse_clock("9pm")) == [
    {"time": "21:00", "label": "9:00 PM", "courts": ["1", "4", "5", "6"]},
    {"time": "22:00", "label": "10:00 PM", "courts": ["1", "4", "5", "6", "7"]}], "open_by_time + --after")
ok(r.open_by_time(after=U.parse_clock("17"), before=U.parse_clock("21:00")) ==
   [{"time": "19:00", "label": "7:00 PM", "courts": ["7"]}], "--after/--before window")
r65 = U.parse_courts_page(mock_site.page(wed, 65, today=date(2026, 11, 17)))
ok(len(r65.courts) == 18 and r65.courts[-1] == "B4B", "wide facility (18 courts)")
odd = [s for s in r65.slots if s.label == "8:00 PM" and s.court == "13"][0]
ok(odd.status == "reserved", "empty-tooltip cell counts as reserved")
empty = U.parse_courts_page(mock_site.page(today + timedelta(days=7), 35, today))
ok(empty.courts == [] and empty.slots == [], "day with no table parses as empty")
try:
    U.parse_courts_page("<html><body>UT EID Login <input type=password></body></html>")
    ok(False, "login page should not parse")
except U.PageChanged:
    ok(True, "a login page is not mistaken for a court page")

live = Path(HERE, "live_gre_squash_2026-11-18.html").read_text()  # real markup, booking links blanked
parts = live.split('href="HREF"')
live = parts[0] + "".join(f'href="reserve_courts.php?id={3080001 + i}&reservationAction=reserve&courtType=SQ"' + p
                          for i, p in enumerate(parts[1:]))
lr = U.parse_courts_page(live)
ok(lr.facility_id == 40 and lr.courts == ["A", "B"], "real page: facility + courts")
ok([(x["label"], x["courts"]) for x in lr.open_by_time(after=17 * 60)] == [("9:00 PM", ["B"]), ("10:00 PM", ["A", "B"])],
   "real page: matches what the live site showed")

print("dates / times / names")
base = date(2026, 11, 17)  # a Tuesday
ok(U.parse_date("tomorrow", base) == date(2026, 11, 18), "tomorrow")
ok(U.parse_date("fri", base) == date(2026, 11, 20), "fri")
ok(U.parse_date("tue", base) == base, "same weekday = today")
ok(U.parse_date("+3", base) == date(2026, 11, 20), "+3")
ok(U.parse_date("11/20", base) == date(2026, 11, 20), "11/20")
ok(U.parse_date("2026-11-21", base) == date(2026, 11, 21), "iso")
ok(U.parse_date("1/3", date(2026, 12, 30)) == date(2027, 1, 3), "1/3 in late Dec -> next year")
ok(U.parse_date("squash", base) is None, "sport isn't a date")
ok(U.parse_clock("5pm") == 1020 and U.parse_clock("17:30") == 1050 and U.parse_clock("12am") == 0, "clock parsing")
ok(U.label_to_minutes("12:00 PM") == 720 and U.label_to_minutes("12:00 AM") == 0, "12 o'clock labels")
ok([f.id for f in U.resolve_facilities(["squash"])] == [28, 40, 55], "squash -> 3 facilities")
ok([f.id for f in U.resolve_facilities(["tennis"])] == [60, 65], "tennis -> 2 facilities")
ok([f.id for f in U.resolve_facilities(["rb", "gre-squash"])] == [35, 50, 40], "mix sport + facility")

print("cli: not signed in")
code, out, err = run("check", "squash")
ok(code == 2 and "utcourts login" in err, "no session -> exit 2 + tells you to log in")
code, out, err = run("check", "+9")
ok(code == 1 and "7 days" in err, "too far ahead is refused up front")

print("cli: simulate the one-time login (a human typing into the SSO page)")
from playwright.sync_api import sync_playwright  # noqa: E402
with sync_playwright() as pw:
    ctx = U._launch(pw, headless=True)
    pg = ctx.new_page()
    pg.goto(U.COURTS_URL)
    pg.fill("input[name=j_username]", "abc123")
    pg.fill("input[type=password]", "not-a-real-password")
    pg.click("button")
    pg.wait_for_url("**/app/myrecsports/**")
    pg.goto(U.COURTS_URL)
    U.remember_browser(ctx, pg.evaluate("navigator.userAgent"))  # what `login` does once it sees the page
    ctx.close()
ok(Path(tmp_home, "session.json").exists(), "login saved a session")

print("cli: app session dies; SSO cookie is a *session* cookie the browser dropped on close")
mock_site.APP_SESSIONS.clear()
mock_site.REQUEST_LOG.clear()
t0 = time.time()
code, out, err = run("check", "rb", "tomorrow", "--json")
ok(code == 0, f"check succeeded (stderr: {err.strip()!r})")
ok("signing back in quietly" in err.lower() or "sign back in quietly" in err.lower(), "said it was re-signing in")
data = json.loads(out)
ok([x["facility_id"] for x in data["results"]] == [35, 50], "json has both racquetball facilities")
ok(any(line.startswith("SSO GET") for line in mock_site.REQUEST_LOG), "went through the SSO")
sess = json.loads(Path(tmp_home, "session.json").read_text())
ok(all(c["domain"] in ("127.0.0.1",) for c in sess["cookies"]) and sess["cookies"], "fast-path cookies are app-only")
ok(any(c["name"] == "idp_session" for c in sess["browser_cookies"]), "SSO session cookie kept for next time")
ok(oct(os.stat(Path(tmp_home, "session.json")).st_mode & 0o777) == "0o600", "session file is private (0600)")
ok("Headless" not in sess["user_agent"], "saved a normal-looking user agent")
print(f"     (took {time.time() - t0:.1f}s)")

print("cli: second check -> fast path, no browser, no SSO")
mock_site.REQUEST_LOG.clear()
t0 = time.time()
code, out, err = run("squash", "tomorrow", "--after", "5pm")
dt = time.time() - t0
ok(code == 0 and err == "", "fast check, nothing on stderr")
ok(not any("SSO" in line for line in mock_site.REQUEST_LOG), "no SSO traffic")
ok("Gregory Gym (GRE) - Squash" in out and "6:00 PM" in out and "A, B" in out, "human output lists open courts")
ok("12:00 PM" not in out, "--after 5pm hides the noon slot")
ok(all("Headless" not in line for line in mock_site.REQUEST_LOG), "fast path sends the saved UA")
print(f"     (took {dt:.2f}s)\n" + "\n".join("     | " + l for l in out.strip().splitlines()))

print("cli: grid view")
code, out, err = run("check", "gre-rb", "tomorrow", "--grid")
ok(code == 0 and "✓" in out and "E" in out and "P" in out and "×" in out, "grid has all symbols")
print("\n".join("     | " + l for l in out.rstrip().splitlines()))

print("cli: app session expires, SSO still alive -> transparent")
mock_site.APP_SESSIONS.clear()
code, out, err = run("check", "tennis", "--days", "2", "--json")
ok(code == 0 and len(json.loads(out)["results"]) == 4, "re-signed in quietly, 2 facilities x 2 days")
mock_site.REQUEST_LOG.clear()
code, out, err = run("status")
ok(code == 0 and "Signed in" in out and not any("SSO" in l for l in mock_site.REQUEST_LOG), "status uses fast path")

print("cli: everything expired -> clear message, exit 2, no hang")
mock_site.APP_SESSIONS.clear()
mock_site.SSO_ALIVE = False
t0 = time.time()
code, out, err = run("check", "squash")
dt = time.time() - t0
ok(code == 2 and "utcourts login" in err, "exit 2 + tells you to run login")
ok(dt < 20, f"gave up quickly ({dt:.1f}s)")
code, out, err = run("status", "--refresh")
ok(code == 2, "status --refresh also exits 2")

print("cli: last bookable day with nothing left / date clamping")
mock_site.SSO_ALIVE = True
code, out, err = run("check", "gre-rb", "+7")
ok(code == 0 and "nothing bookable" in out, "empty day handled")

class _TTY(io.StringIO):
    def isatty(self):
        return True


def run_tty(answers, *argv):
    """Run as if in a terminal, answering prompts from `answers`."""
    real_stdin, real_ask = sys.stdin, U._ask
    sys.stdin = _TTY()
    asked = []
    U._ask = lambda prompt: (asked.append(prompt), answers.pop(0))[1]
    try:
        return (*run(*argv), asked)
    finally:
        sys.stdin, U._ask = real_stdin, real_ask


M = mock_site
print("book")
code, out, err = run("book", "gre-squash", "tomorrow", "6pm")
ok(code == 4 and "--court" in out and "court A" in out and "court B" in out, "two courts open -> lists them, asks for --court")
code, out, err = run("book", "gre-squash", "tomorrow", "6pm", "--court", "A")
ok(code == 4 and "Add --yes" in out and not M.BOOK_REQUESTS, "no terminal + no --yes -> nothing sent")
code, out, err = run("book", "gre-squash", "tomorrow", "6pm", "--court", "a", "--dry-run", "--json")
ok(code == 0 and json.loads(out)["status"] == "dry_run" and json.loads(out)["court"] == "A" and not M.BOOK_REQUESTS,
   "--dry-run (court name case-insensitive)")
code, out, err = run("book", "gre-squash", "tomorrow", "5pm")
ok(code == 4 and "Nothing open" in out and "closest open" in out and "6:00 PM" in out, "not open -> closest open times")
code, out, err = run("book", "gre-squash", "tomorrow", "6pm", "--court", "A", "--yes")
ok(code == 0 and "Booked: Gregory Gym (GRE) - Squash, court A" in out and "confirmed" in out, "books + shows the site's message")
ok(len(M.BOOK_REQUESTS) == 1, "exactly one reserve request")
code, out, err = run("check", "gre-squash", "tomorrow", "--json")
six = [x for x in json.loads(out)["results"][0]["open"] if x["time"] == "18:00"]
ok(six == [{"time": "18:00", "label": "6:00 PM", "courts": ["B"]}], "grid now shows only B open at 6")
code, out, err = run("check", "gre-squash", "tomorrow", "--json", "--all")
ok("reservationAction" not in out and '"href"' not in out, "JSON never includes booking links")

M.REFUSE_BOOKING = "alert-box"
code, out, err = run("book", "gre-squash", "tomorrow", "6pm", "--court", "B", "--yes")
ok(code == 4 and "Not booked" in out and "maximum number" in out, "site refuses (alert box) -> not booked, says why")
M.REFUSE_BOOKING = "js"
code, out, err = run("book", "gre-squash", "tomorrow", "9pm", "--court", "B", "--yes", "--json")
ok(code == 4 and json.loads(out)["status"] == "failed" and "one reservation per day" in out, "site refuses (JS alert) -> failed")
M.REFUSE_BOOKING = None
n = len(M.BOOK_REQUESTS)
code, out, err, asked = run_tty(["n"], "book", "gre-squash", "tomorrow", "9pm")
ok(code == 4 and "Not booked" in out and len(M.BOOK_REQUESTS) == n and "[y/N]" in asked[0], "terminal: answer n -> nothing sent")
M.APP_SESSIONS.clear()  # session dies between the check and the booking
code, out, err, asked = run_tty(["y"], "book", "gre-squash", "tomorrow", "9pm")
ok(code == 0 and "Booked" in out and "sign back in quietly" in err, "terminal: y -> books, even across a quiet re-login")
ok(len(M.BOOK_REQUESTS) == n + 1, "...with exactly one reserve request (no double booking)")
code, out, err, asked = run_tty(["2", "n"], "book", "squash", "tomorrow", "6pm")
ok(len(asked) == 2 and "Which one?" in asked[0] and "Book Gregory Gym (GRE) - Squash, court B" in asked[1],
   "terminal: several facilities -> pick from a numbered list")

print("mine / cancel")
code, out, err = run("mine")
ok(code == 0 and "1. GRE - Squash Court A" in out and "2. GRE - Squash Court B" in out and "9:00 PM" in out,
   "mine lists both bookings, numbered")
ok("no upcoming TeXercise reservations" in out, "mine shows the page's notices")
code, out, err = run("mine", "--json")
ok([r["can_cancel"] for r in json.loads(out)["reservations"]] == [True, True], "mine --json")
code, out, err = run("cancel")
ok(code == 4 and "Say which one" in out, "cancel with no number (no terminal) -> lists, does nothing")
code, out, err = run("cancel", "1")
ok(code == 4 and "Add --yes" in out and len(M.BOOKED) == 2, "cancel without --yes (no terminal) -> nothing cancelled")
code, out, err = run("cancel", "--match", "9:00 pm", "--dry-run")
ok(code == 0 and "Would cancel" in out and "9:00 PM" in out and len(M.BOOKED) == 2, "cancel --match --dry-run")
code, out, err = run("cancel", "--match", "9:00 PM", "--yes")
ok(code == 0 and "Cancelled: GRE - Squash Court B" in out and "cancelled" in out.lower() and len(M.BOOKED) == 1,
   "cancel --match --yes -> cancelled, double-checked")
code, out, err = run("cancel", "--match", "court")
ok(code == 4 and "Add --yes" in out and "Court A" in out and len(M.BOOKED) == 1,
   "--match picks the one left; still needs --yes")
M.CANCEL_STYLE = "form"
M.APP_SESSIONS.clear()
code, out, err, asked = run_tty(["y"], "cancel", "1")
ok(code == 0 and "Cancelled" in out and not M.BOOKED and "Cancel GRE - Squash Court A" in asked[0],
   "form-style cancel button (POST), across a quiet re-login")
code, out, err = run("mine")
ok("No reservations" in out, "mine is empty afterwards")
code, out, err = run("cancel", "1", "--yes")
ok(code == 4 and "no reservations to cancel" in out, "nothing to cancel")
M.CANCEL_STYLE = "link"

js_only = ('<html><body><a href="/logout">x</a><ul><li>RSC - Racquetball Court 3 Fri 11/20 7:00 PM '
           '<a href="#" onclick="cancelRes(77)">Cancel</a></li></ul></body></html>')
items, _ = U.parse_reservations(js_only)
ok(len(items) == 1 and items[0].cancel_method is None and "Court 3" in items[0].text,
   "JS-only cancel button is listed but marked 'cancel on the website'")
cards = ('<html><body><a href="/logout">x</a>'
         '<div class="card"><div class="card-body"><div class="res"><b>GRE Squash</b> Court A, 11/19 6:00 PM '
         '<a class="btn" href="myreservations.php?id=1&amp;do=cancel">Cancel Reservation</a></div>'
         '<div class="res"><b>GRE Squash</b> Court B, 11/19 9:00 PM '
         '<a class="btn" href="myreservations.php?id=2&amp;do=cancel">Cancel Reservation</a></div></div></div></body></html>')
items, _ = U.parse_reservations(cards, "https://x/app/myrecsports/myreservations.php")
ok([i.text for i in items] == ["GRE Squash Court A, 11/19 6:00 PM", "GRE Squash Court B, 11/19 9:00 PM"]
   and items[1].cancel_url.endswith("id=2&do=cancel"), "card layout: each reservation found separately")

one_card = ('<html><body><a href="/logout">x</a><div class="card"><h5 class="card-header">Court Reservations</h5>'
            '<div class="card-body"><div><strong>BEL - Squash</strong> Court 932</div><div>Thu 11/19/2026 7:00 PM</div>'
            '<a class="btn" href="myreservations.php?id=9&amp;action=cancel">Cancel</a></div></div>'
            '<div class="card"><h5 class="card-header">TeXercise</h5><p>You have no upcoming TeXercise reservations.</p>'
            '</div></body></html>')
items, notes = U.parse_reservations(one_card, "https://x/app/myrecsports/myreservations.php")
ok([i.text for i in items] == ["BEL - Squash Court 932 Thu 11/19/2026 7:00 PM"] and notes,
   "single reservation in a card: text stays inside its card")

print("help")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    U.main(["help"])
ok("book" in buf.getvalue() and "cancel" in buf.getvalue() and "exit codes" in buf.getvalue(), "utcourts help")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    U.main(["help", "book"])
ok("--dry-run" in buf.getvalue() and "--court" in buf.getvalue(), "utcourts help book")

print("cli: facilities + logout")
code, out, err = run("facilities")
ok(code == 0 and "gre-squash" in out, "facilities list")
code, out, err = run("logout")
ok(code == 0 and not Path(tmp_home, "session.json").exists() and not Path(tmp_home, "profile").exists(), "logout wipes both")

print(f"\nall {PASS} checks passed")
