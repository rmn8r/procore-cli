"""Print session cookie names/domains (NO values) + hunt for a projects-list
endpoint and any CSRF-bearing cookie, so we know how to authenticate the client."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "storage_state.json"
CAPTURE = sorted((ROOT / "captures").glob("capture-*.jsonl"))[-1]

print("=== storage_state cookies (names + domains only) ===")
state = json.loads(STATE.read_text(encoding="utf-8"))
for c in state.get("cookies", []):
    name = c["name"]
    flags = []
    if c.get("httpOnly"):
        flags.append("httpOnly")
    if c.get("secure"):
        flags.append("secure")
    marker = "  <-- looks CSRF-ish" if "csrf" in name.lower() or "token" in name.lower() else ""
    print(f"  {c['domain']:28} {name:32} [{','.join(flags)}]{marker}")

origins = state.get("origins", [])
print(f"\n=== localStorage origins: {[o['origin'] for o in origins]} ===")
for o in origins:
    for item in o.get("localStorage", []):
        n = item["name"]
        if "csrf" in n.lower() or "token" in n.lower():
            print(f"  localStorage {o['origin']} :: {n}  <-- token-ish")

print("\n=== hunting for a projects-LIST endpoint in capture ===")
hosts = set()
seen = set()
for line in CAPTURE.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    rec = json.loads(line)
    u = urlparse(rec["url"])
    hosts.add(u.hostname)
    p = u.path
    # list-style: ends in /projects or /projects with query, not /projects/{id}
    if p.rstrip("/").endswith("/projects") or "list_of_projects" in p or "project_list" in p:
        if p not in seen:
            seen.add(p)
            print(f"  {rec['method']} {rec['url'][:160]}  [{rec['status']}]")

print(f"\n=== all hosts in capture: {sorted(h for h in hosts if h)} ===")
