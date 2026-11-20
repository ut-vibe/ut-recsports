# ut-recsports

A Claude Code plugin and a standalone CLI (`utcourts`) for UT Austin RecSports courts: check what's open, book a court, see your reservations, and cancel them. It covers squash, racquetball/handball, pickleball and tennis at BEL, GRE, RSC, CCF and Whitaker.

Everyone signs in with their own UT EID. Nothing is shared between people.

![utcourts: check open tennis courts, book one, see it in your reservations](demo/demo.gif)

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

You need [uv](https://docs.astral.sh/uv/) (`brew install uv`) and Google Chrome. The script installs its own Python packages on first run. No uv? Run `pip install httpx beautifulsoup4 playwright` (add `keyring` to save your password), then use `python3 utcourts.py`.

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

`book` and `cancel` always show exactly what they're about to do and ask `[y/N]`. Afterward they re-check the site to confirm it worked. `--dry-run` shows what would happen without doing it.

```
$ utcourts squash tomorrow --after 8pm
Squash · Tomorrow, Wed Nov 18 · from 8 PM

  BEL Squash  bel-squash
     8 PM  932
     9 PM  932

  GRE Squash  gre-squash
     9 PM  B
    10 PM  A  B

Book one: utcourts book bel-squash tomorrow 8pm --court 932
```

Commands also take `--json`, for scripts and agents.

## How it avoids logging in all the time

`utcourts login` signs you in inside a separate, private Chrome profile (`~/.config/utcourts/`). Its cookies are saved where only your user can read them, so checks are quick plain requests. When the RecSports session expires, it quietly re-signs in through UT's single sign-on in a hidden browser. You only see the sign-in window again once UT's SSO and Duo have fully expired. It saves the SSO cookies itself too, because browsers drop those when they close.

UT's SSO session still runs out after a while, and then it wants your password again. To cover that, utcourts saves the EID and password you type in the sign-in window to your system keychain (the macOS Keychain on a Mac), and only after the sign-in works. The window says so before you type. After that, when UT asks again, the hidden browser types them in for you:

- If Duo still trusts the browser (say yes to "Trust this browser"; UT's lasts about 30 days), that's it. Nothing to do.
- If Duo wants a push, the terminal says "Approve the Duo push on your phone". You tap approve; no window. With Verified Duo Push it also shows the code to type in.
- The saved password is only typed in when you run a command yourself in a terminal, or run `utcourts login`. Commands run with `--no-login` or from a script never trigger a push.
- If UT turns the saved password down (you changed it, say), utcourts forgets it right away and never retries, so it can't lock your account. The next password you type in the window is saved instead.
- If Duo shows a screen utcourts doesn't recognize, it opens the window as before, with your EID and password already filled in.

Don't want your password saved? Run `utcourts login --no-save-password`. That removes a saved one, stays off (even after `logout`), and `--save-password` turns it back on. `utcourts logout` removes the password from the keychain along with everything else. Keep in mind that while it's saved, anyone who can use your computer account can get as far as Duo as you.

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
demo/demo.tape                               the demo above (`vhs demo/demo.tape`; books a real court)
```

To use the skill in just one repo without a plugin, copy `plugins/ut-recsports/skills/courts/` to `<that repo>/.claude/skills/courts/` and commit it.

## Tests

The suite needs the Python packages and Playwright's own Chromium: `pip install httpx beautifulsoup4 playwright keyring && python3 -m playwright install chromium`. It uses an in-memory keyring (`tests/fake_keyring.py`), never your real one.

```bash
python3 tests/run_tests.py                         # 121 checks against a mock RecSports + UT SSO + Duo
UTCOURTS_BROWSER=chrome python3 tests/run_tests.py # same, with your installed Chrome
xvfb-run -a python3 tests/test_login_window.py     # the visible sign-in window (Linux)
claude plugin validate .                           # marketplace + plugin manifests
```
