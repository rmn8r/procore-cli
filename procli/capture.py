"""
Phase 1 discovery harness for the Procore CLI.

Opens a real Chromium window. YOU log in by hand (SSO / MFA included) -- your
credentials are never seen by this script or by Claude. While you click around
Procore, every XHR / fetch API call is recorded so we can reverse-engineer the
internal API. Your logged-in session is saved to storage_state.json so the CLI
can reuse it later without a browser.

Usage:
    python -m procli.capture              # login + record a browsing session
    python -m procli.capture --headless   # (not for login; session must exist)

While the browser is open:
    1. Log in to Procore completely (until you see your project dashboard).
    2. Visit every screen you want the CLI to support -- open a project, click
       into RFIs, Submittals, Drawings, Daily Log, Directory, Budget, etc.
       Open a few individual records too (one RFI, one submittal) so we capture
       both "list" and "detail" endpoints.
    3. Come back to this terminal and press ENTER to save + exit.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "storage_state.json"
CAPTURE_DIR = ROOT / "captures"

START_URL = "https://login.procore.com/"

# Header values we mask before writing to disk. We keep the header *names* (so we
# know which auth mechanism Procore uses) but never persist the secret values.
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-csrf-token"}

# Only record calls that look like API traffic, not HTML/CSS/images/fonts.
API_RESOURCE_TYPES = {"xhr", "fetch"}


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            out[k] = f"<masked len={len(v)}>"
        else:
            out[k] = v
    return out


def _looks_like_api(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return "procore.com" in host


def run(headless: bool = False) -> None:
    CAPTURE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = CAPTURE_DIR / f"capture-{stamp}.jsonl"
    # Stream each call to disk immediately so nothing is ever lost, even if the
    # process is killed or the terminal can't be found.
    sink = out_path.open("w", encoding="utf-8", buffering=1)
    counter = {"n": 0, "saved_session": False}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context_kwargs = {}
        if STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STATE_PATH)
            print(f"[capture] reusing saved session from {STATE_PATH.name}")
        context = browser.new_context(**context_kwargs)

        def save_session():
            try:
                context.storage_state(path=str(STATE_PATH))
                return True
            except Exception:
                return False  # context may be tearing down

        def on_response(response):
            req = response.request
            if req.resource_type not in API_RESOURCE_TYPES:
                return
            if not _looks_like_api(req.url):
                return
            rec: dict = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "status": response.status,
                "request_headers": _mask_headers(req.headers),
                "response_headers": _mask_headers(response.headers),
            }
            if req.post_data:
                rec["request_body"] = req.post_data[:20000]
            # Read bytes EAGERLY here: in an SPA, fast route changes purge the
            # response resource, so a lazy .text() later fails with
            # "response ... navigated away from". response.body() forces the
            # buffer now, while we're still in the response handler.
            ctype = response.headers.get("content-type", "")
            if "application/json" in ctype:
                try:
                    raw = response.body()
                    body = raw.decode("utf-8", errors="replace")
                    rec["response_body_sample"] = body[:200000]
                    rec["response_body_truncated"] = len(body) > 200000
                except Exception as exc:
                    rec["response_body_error"] = str(exc)

            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
            counter["n"] += 1
            print(f"  {response.status} {req.method:6} {req.url[:110]}")

            # Persist the session opportunistically: once right after login, then
            # periodically, so a valid storage_state is always on disk.
            if not counter["saved_session"] and "/rest/v1.0/me" in req.url and response.status == 200:
                if save_session():
                    counter["saved_session"] = True
                    print("  [capture] session saved (logged in)")
            elif counter["n"] % 25 == 0:
                save_session()

        context.on("response", on_response)

        page = context.new_page()
        page.goto(START_URL)

        print("\n" + "=" * 72)
        print(" 1. Log in to Procore in the browser window.")
        print(" 2. Visit every screen you want the CLI to support, and open a few")
        print("    individual records (RFI, submittal, correspondence item, ...).")
        print(" 3. When done, just CLOSE THE BROWSER WINDOW to finish.")
        print("    Captures stream to disk live, so nothing is lost.")
        print("=" * 72)

        if headless:
            # No window to close; wait on the terminal instead.
            try:
                input("\n[capture] Press ENTER to finish... ")
            except (EOFError, KeyboardInterrupt):
                pass
        else:
            # Block until the user closes the browser -- no terminal needed.
            # Re-save the session every ~10s so the on-disk cookies stay current
            # (Procore rotates _session_id), keeping the saved session as fresh
            # as possible right up to the moment the window closes.
            try:
                ticks = 0
                while browser.is_connected():
                    page.wait_for_timeout(1000)
                    ticks += 1
                    if ticks % 10 == 0:
                        save_session()
            except Exception:
                pass  # page/browser went away -> we're done

        save_session()
        if browser.is_connected():
            browser.close()

    sink.close()
    print(f"\n[capture] saved {counter['n']} API calls -> {out_path}")
    print(f"[capture] session -> {STATE_PATH}")
    print("[capture] Send me the capture-*.jsonl file to build/verify the client.")


if __name__ == "__main__":
    run(headless="--headless" in sys.argv)
