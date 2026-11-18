---
name: courts
description: Check, book, list or cancel UT Austin RecSports court reservations (squash, racquetball/handball, pickleball, tennis) at apps.rs.utexas.edu. Use when someone asks which courts are open, wants to book a court, or wants to see or cancel their reservations.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/utcourts.py check *) Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/utcourts.py mine *) Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/utcourts.py facilities *) Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/utcourts.py status *)
---

# UT RecSports courts

Works with the court grid at https://apps.rs.utexas.edu/app/myrecsports/reserve_courts.php and the
My Reservations page. Booking and cancelling change the person's real account, so **never book or
cancel without the person saying yes to that exact slot or reservation in this conversation.**

Pick the route that matches your tools.

## Route A: a shell on the person's computer (Claude Code)

Run the bundled CLI. Always pass `--json --no-login` so it never blocks on a window:

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/utcourts.py" <command> ... --json --no-login
```

No `uv`? Use `python3 "${CLAUDE_SKILL_DIR}/scripts/utcourts.py"` after
`pip install httpx beautifulsoup4 playwright`. `utcourts.py help <command>` prints the details.

### Check
`check <sport|facility...> <date> [--after 5pm] [--before 9pm] [--days N]`
- Words go in any order: `check squash tomorrow --after 6pm`.
- Sports: `squash`, `racquetball` (`rb`, `handball`), `pickleball` (`pb`), `tennis`. Leave it out for every facility.
- Dates: `today`, `tomorrow`, `fri`, `+2`, `11/20`, `2026-11-20`. Only today through 7 days ahead.
- JSON: `{"results": [{"date", "facility_id", "facility", "courts", "open": [{"time": "21:00", "label": "9:00 PM", "courts": ["B"]}]}]}`

### Book (two steps, always)
1. Dry run: `book <facility> <date> <time> [--court X] --dry-run --json`.
   - `status: "dry_run"` gives the exact facility, court, date and time. Show it to the person and ask whether to book it.
   - `status: "ambiguous"` lists `options`. Ask which one, then dry-run again with `--court` and a facility short name.
   - `status: "not_open"` gives `closest_open` times per facility. Offer those instead.
2. Only after a clear yes: run the same command with `--yes --json` (no `--dry-run`).
   - `booked`: done. The CLI re-checks the grid to confirm.
   - `failed`: not booked. Pass on `messages` (RecSports' own words, e.g. a daily limit).
   - `taken`: someone else got it first.
   - `unclear`: run `mine --json` and tell the person what you see.

### See reservations
`mine --json` returns `{"reservations": [{"index", "text", "can_cancel"}], "notices": [...]}`.

### Cancel (two steps, always)
1. Run `mine --json`, pick the reservation the person means, and dry-run it by its text:
   `cancel --match "<words from its text, e.g. date and time>" --dry-run --json`.
   Show the matched `reservation` text and ask whether to cancel it.
2. Only after a clear yes: run the same command with `--yes --json`. Use `--match`, not the number,
   because the numbers can shift if the list changes. Results: `cancelled` (confirmed gone),
   `failed`, `unclear`, `not_found`, or `unsupported` (that reservation's cancel button runs page
   JavaScript; send the person to the My Reservations page).

### Exit codes and signing in
- `0` ok · `1` bad input (fix the arguments) · `3` network trouble or the page changed.
- `4`: nothing was done (not open, ambiguous, not confirmed, or RecSports said no). Read the JSON.
- `2`: needs a fresh UT sign-in. Tell the person a Chrome window is about to open for their EID and Duo, then run `uv run "${CLAUDE_SKILL_DIR}/scripts/utcourts.py" login` (no `--json`; it waits up to 5 minutes). Then retry.

Each person signs in with their own EID. Sessions are saved per user in `~/.config/utcourts/`.

## Route B: Claude in Chrome (browser tools, no shell)

The person's Chrome is already signed in, so read the pages from a tab on `apps.rs.utexas.edu`.
Use their tab if it's on that site; otherwise navigate the current tab to the court page above.
Run this with the javascript tool, editing the last line:

```js
async function courts(dateMDY, facIds, afterMin = 0, beforeMin = 24 * 60) {
  const out = [];
  for (const fac of facIds) {
    const u = new URL('/app/myrecsports/reserve_courts.php', location.origin);
    u.searchParams.set('date', dateMDY); u.searchParams.set('facility_id', fac);
    let doc;
    try {
      const r = await fetch(u, {credentials: 'same-origin'});
      doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { return 'NOT SIGNED IN (request was redirected to UT login)'; }
    if (!doc.getElementById('form_courts')) return 'NOT SIGNED IN';
    const name = doc.querySelector(`#facility_id option[value="${fac}"]`)?.textContent.trim();
    const day = doc.getElementById('date')?.value;
    const courtNames = [...doc.querySelectorAll('table th')].slice(1).map(th => th.textContent.trim());
    const lines = [];
    for (const tr of doc.querySelectorAll('table tr')) {
      const tds = [...tr.children].filter(c => c.tagName === 'TD');
      if (!tds.length) continue;
      const label = tds[0].textContent.trim();
      const m = label.match(/(\d+):(\d+)\s*([AP])M/i);
      if (!m) continue;
      const mins = ((+m[1] % 12) + (m[3].toUpperCase() === 'P' ? 12 : 0)) * 60 + +m[2];
      if (mins < afterMin || mins >= beforeMin) continue;
      const open = tds.slice(1).map((td, i) => td.querySelector('a') ? courtNames[i] : null).filter(Boolean);
      if (open.length) lines.push(`${label}: ${open.join(', ')}`);
    }
    out.push(`${name} — ${day}\n  ` + (lines.join('\n  ') || 'no open courts in that window'));
  }
  return out.join('\n');
}
await courts('11/18/2026', [28, 40, 55], 17 * 60);   // date MM/DD/YYYY, facility ids, after (minutes)
```

- If it returns `NOT SIGNED IN`, ask the person to sign in on that tab, then run it again.
- Return only parsed text. The javascript tool blocks results that contain raw links or query strings.
- Check the date in each header line. RecSports quietly swaps an out-of-range date for the nearest allowed one.
- **Booking or cancelling in Chrome:** don't click "+" or Cancel yourself; those buttons open a confirm dialog that blocks the browser tools. Navigate the tab to the right page (the court page with `?date=MM/DD/YYYY&facility_id=<id>`, or `/app/myrecsports/myreservations.php`) and tell the person exactly which button to click.

## Facilities

| id | facility | short name |
|---|---|---|
| 28 | Bellmont Hall (BEL) - Squash | bel-squash |
| 40 | Gregory Gym (GRE) - Squash | gre-squash |
| 55 | Recreational Sports Center (RSC) - Squash | rsc-squash |
| 35 | Gregory Gym (GRE) - Racquetball / Handball | gre-rb |
| 50 | Recreational Sports Center (RSC) - Racquetball / Handball | rsc-rb |
| 30 | Caven-Clark Courts (CCF) - Pickleball Only | ccf-pickleball |
| 60 | Whitaker Courts (WC) - Tennis Only | wc-tennis |
| 65 | Whitaker Courts (WC) - Pickleball / Tennis | wc-pickleball |

By sport: squash = 28, 40, 55 · racquetball/handball = 35, 50 · pickleball = 30, 65 · tennis = 60, 65.

## Reading the grid

- An open court has a green "+" link. Opening that link **books it immediately**; never fetch or
  follow it except through `book --yes` after the person said yes.
- Not open: `Reserved`, `Event Conflict: <name>`, `Facility Closed`, or `Faculty/Staff Priority Hours` (students can't book those).
- For today, the page only lists the hours that are left.

## How to answer

Lead with the answer, grouped by facility, for example: "Squash tomorrow after 5: RSC courts 9 and
10 every hour 5–10 PM; GRE court B at 9 PM, A and B at 10 PM." If nothing is open, say so and give
the closest open times. Don't dump the whole grid unless asked.
