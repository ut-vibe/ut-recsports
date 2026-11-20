"""Visible-window login, run under xvfb: xvfb-run -a python3 tests/test_login_window.py"""
import contextlib, io, json, os, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_site
app_port, _ = mock_site.start()
home = tempfile.mkdtemp()
os.environ.update(UTCOURTS_BASE_URL=f"http://127.0.0.1:{app_port}", UTCOURTS_HOME=home, UTCOURTS_BROWSER="chromium",
                  PYTHON_KEYRING_BACKEND="fake_keyring.MemoryKeyring")  # never the real Keychain
sys.path.insert(0, str(HERE.parent / "plugins/ut-recsports/skills/courts/scripts"))
import utcourts as U

mock_site.HUMAN_AUTOSUBMIT = True
err = io.StringIO()
t0 = time.time()
with contextlib.redirect_stderr(err):
    code = U.main(["login"])
print(err.getvalue())
assert code == 0, code
assert "Signed in" in err.getvalue()
sess = json.loads(Path(home, "session.json").read_text())
assert any(c["name"] == "PHPSESSID" for c in sess["cookies"])
assert any(c["name"] == "idp_session" for c in sess["browser_cookies"])
assert "Headless" not in sess["user_agent"]
assert U.saved_credentials() == ("abc123", "hunter2"), "the password typed in the window is saved"
print(f"visible-window login ok ({time.time()-t0:.1f}s); landed via app home -> courts nudge:",
      any("(home)" in l for l in mock_site.REQUEST_LOG))
