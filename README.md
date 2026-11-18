# ut-recsports

A Claude Code plugin and a standalone CLI (`utcourts`) for UT Austin RecSports courts: check what's open, book a court, see your reservations, and cancel them. It covers squash, racquetball/handball, pickleball and tennis at BEL, GRE, RSC, CCF and Whitaker.

Everyone signs in with their own UT EID. Nothing is shared between people.

## Install (for anyone)

**As a Claude Code plugin** (gives you the `/ut-recsports:courts` skill):

```bash
claude plugin marketplace add ut-vibe/ut-recsports
claude plugin install ut-recsports@ut-recsports
```

Then ask Claude things like "any squash courts open tomorrow after 6?" or "book GRE squash court B at 9pm Friday". Claude always shows you the exact slot and waits for your yes before booking or cancelling anything.

**As a plain CLI:**

```bash
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/ut-vibe/ut-recsports/main/plugins/ut-recsports/skills/courts/scripts/utcourts.py -o ~/.local/bin/utcourts
chmod +x ~/.local/bin/utcourts
utcourts login           # Chrome window: EID + Duo, once
```

You need [uv](https://docs.astral.sh/uv/) (`brew install uv`) and Google Chrome. The script installs its own Python packages on first run. No uv? Run `pip install httpx beautifulsoup4 playwright`, then use `python3 utcourts.py`.

## Commands

```
utcourts squash tomorrow --after 6pm           check (the default command)
utcourts check rb fri --grid                   full grid: ✓ open · reserved E event P priority × closed
utcourts book gre-squash tomorrow 9pm --court B
utcourts book squash fri 6pm                   several fit? pick from a list
utcourts mine                                  your reservations, numbered
utcourts cancel 1                              or: utcourts cancel --match "11/18 9:00 PM"
utcourts help book                             details for any command
```

`book` and `cancel` always show exactly what they're about to do and ask `[y/N]`. Afterward they re-check the site to confirm it worked. `--dry-run` shows what would happen without doing it. Scripts and agents have no prompt, so they must pass `--yes`.

Exit codes: `0` ok · `1` bad input · `2` needs a fresh sign-in · `3` network trouble or page changed · `4` nothing done (not open, not confirmed, ambiguous, or RecSports said no).

## How it avoids logging in all the time

`utcourts login` signs you in inside a separate, private Chrome profile (`~/.config/utcourts/`). Its cookies are saved where only your user can read them, so checks are quick plain requests. When the RecSports session expires, it quietly re-signs in through UT's single sign-on in a hidden browser. You only see the sign-in window again once UT's SSO and Duo have fully expired. It saves the SSO cookies itself too, because browsers drop those when they close.

## Status

- **Checking:** tested against real RecSports pages (the parser test includes one live page with its booking links blanked out).
- **Booking:** uses the same "+" link the website does, then confirms the court no longer shows as open. Tested against a mock built from the live markup and used to book a real court on the live site.
- **mine:** tested against the real My Reservations page with a real court booking.
- **cancel:** finds reservations by their cancel buttons (links or forms) and confirms a cancel by checking the reservation is gone. Tested against the mock, but not yet used to cancel a real reservation.

## Repo layout

```
.claude-plugin/marketplace.json              makes this repo installable as a marketplace
plugins/ut-recsports/.claude-plugin/plugin.json
plugins/ut-recsports/skills/courts/SKILL.md  the skill (CLI route + Claude in Chrome route)
plugins/ut-recsports/skills/courts/scripts/utcourts.py   the CLI, one file
tests/                                       mock RecSports + UT SSO, and the test suite
```

To use the skill in just one repo without a plugin, copy `plugins/ut-recsports/skills/courts/` to `<that repo>/.claude/skills/courts/` and commit it.

## Tests

The suite needs the Python packages and Playwright's own Chromium: `pip install httpx beautifulsoup4 playwright && python3 -m playwright install chromium`.

```bash
python3 tests/run_tests.py                         # 82 checks against a mock RecSports + UT SSO
xvfb-run -a python3 tests/test_login_window.py     # the visible sign-in window (Linux)
claude plugin validate .                           # marketplace + plugin manifests
```
