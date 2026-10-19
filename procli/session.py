"""
Authenticated HTTP session for the Procore CLI.

Reuses the cookies saved by `procli.capture` (storage_state.json) to talk to
Procore's internal REST API directly with httpx -- no browser at runtime.

Auth model (reverse-engineered from the capture):
  * `_session_id` cookie on us02.procore.com carries the logged-in session.
  * `csrf_token` cookie is echoed back in the `x-csrf-token` header (double-submit
    CSRF). Required for writes (POST/PUT/PATCH/DELETE); GETs work without it.
  * Some endpoints also want a `procore-company-id` header.

If the session has expired, calls return HTTP 401/302 -> re-run `procli.capture`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "storage_state.json"

BASE_URL = "https://us02.procore.com"  # fallback only; real host auto-detected below

# Procore shards per region (us02, us03, eu1, au1, …). The API lives on the shard,
# not on app.procore.com (the login redirector). Auto-detect from the saved
# session's cookies so this works for any user/region; override with PROCORE_HOST.
_SHARD_RE = re.compile(r"^(?:[a-z]{2}\d+)\.procore\.com$", re.I)


def _detect_base_url(state: dict) -> str:
    env = os.environ.get("PROCORE_HOST")
    if env:
        return env if env.startswith("http") else f"https://{env}"
    shard_hosts = [(c.get("name"), (c.get("domain") or "").lstrip("."))
                   for c in state.get("cookies", [])]
    shard_hosts = [(n, d) for n, d in shard_hosts if _SHARD_RE.match(d)]
    for name, dom in shard_hosts:          # prefer the shard carrying the session
        if name == "_session_id":
            return f"https://{dom}"
    if shard_hosts:
        return f"https://{shard_hosts[0][1]}"
    return BASE_URL


class ProcoreAuthError(RuntimeError):
    """Session missing or expired; re-run `python -m procli.capture`."""


class ProcoreSafetyError(RuntimeError):
    """A destructive operation was blocked by the safety guard."""


# Verbs that can destroy data in Procore. Blocked everywhere (CLI, MCP, raw)
# unless PROCORE_ALLOW_DELETE=1 is set — the MCP server is launched WITHOUT it,
# so no Cowork/MCP call can ever delete. A human can opt in per-invocation on the
# CLI. This is a hard guard at the transport layer, below every command/tool.
_BLOCKED_METHODS = {"DELETE"}


class ProcoreSession:
    def __init__(self, state_path: Path = STATE_PATH, base_url: str | None = None):
        if not state_path.exists():
            raise ProcoreAuthError(
                f"{state_path.name} not found. Run `python -m procli.capture` first."
            )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        base_url = base_url or _detect_base_url(state)

        cookies = httpx.Cookies()
        csrf_token: str | None = None
        for c in state.get("cookies", []):
            cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
            if c["name"] == "csrf_token":
                csrf_token = c["value"]
        self._csrf_token = csrf_token

        headers = {
            "accept": "application/json, text/plain, */*",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
            ),
        }
        if csrf_token:
            headers["x-csrf-token"] = csrf_token

        self._client = httpx.Client(
            base_url=base_url,
            cookies=cookies,
            headers=headers,
            timeout=30.0,
            follow_redirects=False,  # a 302 to /login means the session died
        )

    # -- low-level ---------------------------------------------------------
    def request(self, method: str, path: str, *, company_id: int | str | None = None,
                params: dict | None = None, json_body: dict | None = None) -> httpx.Response:
        if method.upper() in _BLOCKED_METHODS and not os.environ.get("PROCORE_ALLOW_DELETE"):
            raise ProcoreSafetyError(
                f"{method.upper()} blocked by the Procore safety guard — this MCP/CLI "
                f"cannot delete anything in Procore. (A human may override for a single "
                f"CLI run by setting PROCORE_ALLOW_DELETE=1; the MCP server never sets it.)"
            )
        headers = {}
        if company_id is not None:
            headers["procore-company-id"] = str(company_id)
        if json_body is not None:
            headers["content-type"] = "application/json"
        resp = self._client.request(
            method, path, params=params, json=json_body, headers=headers or None
        )
        # Procore signals an expired/idle-timed-out session two ways:
        #   * a 3xx redirect to a login URL, or
        #   * a 401 with body {"message":"You'll need to login first."}
        # Note: /rest/v1.0/me and /companies keep working after project-scoped
        # access has already lapsed, so don't rely on those to detect expiry.
        redirected_to_login = (
            resp.status_code in (301, 302)
            and "login" in resp.headers.get("location", "")
        )
        login_required = (
            resp.status_code == 401
            and "login" in resp.text.lower()
        )
        if redirected_to_login or login_required:
            raise ProcoreAuthError(
                "Procore session expired (idle timeout). "
                "Re-run `python -m procli.capture` to refresh it."
            )
        return resp

    def get(self, path: str, **kw) -> httpx.Response:
        return self.request("GET", path, **kw)

    def get_json(self, path: str, **kw):
        resp = self.get(path, **kw)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
