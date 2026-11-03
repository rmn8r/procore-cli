"""
Interactive session refresh for the Procore CLI.

When the stored session lapses, open a real browser window so the user can
complete SSO. Because the browser profile still holds the Okta cookies, this is
usually a *silent* redirect (no typing). We poll until a project-scoped API call
actually succeeds with the freshly saved cookies, then close the window and
return -- so the CLI can transparently retry the command that failed.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import os
import time

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "storage_state.json"
LOCK_PATH = ROOT / ".login.lock"

# app.procore.com redirects an unauthenticated visit through login -> Okta and
# back, minting a fresh regional (us02) session on the way.
APP_URL = "https://app.procore.com/"


def _acquire_login_lock(stale_s: int = 300) -> bool:
    """Atomically claim the sole login slot. Returns False if another process is
    already showing a login window (so we don't spawn a second browser — the
    cause of the flickering tabs while typing credentials)."""
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode()); os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - LOCK_PATH.stat().st_mtime > stale_s:
                LOCK_PATH.unlink(missing_ok=True)      # stale lock -> reclaim
                return _acquire_login_lock(stale_s)
        except FileNotFoundError:
            return _acquire_login_lock(stale_s)
        return False


def _release_login_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _session_has_project_scope() -> bool:
    """True only if the cookies pass a real PROJECT-TOOL call (an RFIs fetch).

    IMPORTANT: `/me`, `/companies`, and even `/companies/{id}/projects` can stay
    authorized in a tier that OUTLIVES the project-tool endpoints (rfis, submittals,
    inspections, …). Probing only the projects list gives a FALSE POSITIVE — login
    reports 'refreshed' while every tool call still 401s. So we probe the deepest
    tier (an actual `/projects/{id}/rfis` call)."""
    if not STATE_PATH.exists():
        return False
    from procli.client import ProcoreClient
    from procli.session import ProcoreAuthError

    try:
        with ProcoreClient() as c:
            comps = c.companies()
            if not comps:
                return False
            ordered = sorted(comps, key=lambda x: 0 if x.get("my_company") else 1)
            projects_ok = False
            for comp in ordered:
                try:
                    projs = c.projects(comp["id"])
                except ProcoreAuthError:
                    return False
                except Exception:
                    continue
                projects_ok = True
                if not projs:
                    continue
                try:                       # probe the real tool tier
                    c.rfis(projs[0]["id"], comp["id"], per_page=1)
                    return True
                except ProcoreAuthError:
                    return False           # tools are 401 -> session NOT usable
                except Exception:
                    continue               # rfis not available here -> try next
            return projects_ok             # couldn't confirm rfis anywhere; trust tier-2
    except ProcoreAuthError:
        return False
    except Exception:
        return False


def clear_session() -> bool:
    """Hard logout: drop the saved session so the next login does a FULL fresh SSO
    (not a no-op refresh). The old state is kept as `storage_state.json.loggedout`
    in case it's needed. Local file op only — never touches Procore."""
    had = STATE_PATH.exists()
    if had:
        try:
            STATE_PATH.replace(STATE_PATH.with_name(STATE_PATH.name + ".loggedout"))
        except Exception:
            try:
                STATE_PATH.unlink()
            except Exception:
                pass
    for lock in (LOCK_PATH,):
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass
    return had


def refresh_session(timeout_s: int = 180, headless: bool = False,
                    poll_s: float = 2.0) -> bool:
    """Open a window for SSO; return True once a project-scoped call succeeds.

    Single-flight: if the session is already valid, returns immediately without a
    browser; if another process is already showing a login window, waits for that
    one to finish instead of opening a second (prevents the tab-flicker)."""
    # Already good? Don't open anything.
    if _session_has_project_scope():
        return True

    if not _acquire_login_lock():
        # Someone else is logging in — wait for their window to succeed.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            if _session_has_project_scope():
                return True
        return False

    try:
        return _do_browser_login(timeout_s, headless, poll_s)
    finally:
        _release_login_lock()


def _do_browser_login(timeout_s: int, headless: bool, poll_s: float) -> bool:
    ok = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx_kwargs = {}
        if STATE_PATH.exists():
            ctx_kwargs["storage_state"] = str(STATE_PATH)
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        page.goto(APP_URL)

        deadline = time.monotonic() + timeout_s
        while browser.is_connected() and time.monotonic() < deadline:
            page.wait_for_timeout(int(poll_s * 1000))
            try:
                context.storage_state(path=str(STATE_PATH))
            except Exception:
                break  # context tearing down (user closed the window)
            if _session_has_project_scope():
                # One more save to capture the very latest cookies, then done.
                try:
                    context.storage_state(path=str(STATE_PATH))
                except Exception:
                    pass
                ok = True
                break

        if browser.is_connected():
            browser.close()
    return ok


if __name__ == "__main__":
    print("[auth] Opening a browser to refresh your Procore session...")
    print("[auth] refreshed" if refresh_session() else "[auth] not refreshed (timed out / cancelled)")
