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
    """True only if the freshly saved cookies pass a PROJECT-scoped call.
    (`/me` and `/companies` keep working after project scope lapses, so they
    can't be trusted as a liveness check.)"""
    if not STATE_PATH.exists():
        return False
    # Imported lazily so a stale module-load order can't break capture-only use.
    from procli.client import ProcoreClient
    from procli.session import ProcoreAuthError

    try:
        with ProcoreClient() as c:
            comps = c.companies()
            if not comps:
                return False
            # Prefer the user's own company if flagged; else just try each until
            # one answers (a company with no project access raises non-auth).
            ordered = sorted(comps, key=lambda x: 0 if x.get("my_company") else 1)
            for comp in ordered:
                try:
                    c.projects(comp["id"])
                    return True          # a project-scoped call succeeded
                except ProcoreAuthError:
                    return False          # session genuinely not valid yet
                except Exception:
                    continue              # permission/other -> try next company
            return False
    except ProcoreAuthError:
        return False
    except Exception:
        return False


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
