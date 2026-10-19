"""Ad-hoc: dump the mechanics we need to build the client, without leaking data.

Prints, for a set of interesting endpoints: full URL (with query string), the
request header NAMES present (values masked already), and the top-level keys /
shape of the JSON response sample. No field values from responses are printed
except list length and key names.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
CAPTURE = sorted((ROOT / "captures").glob("capture-*.jsonl"))[-1]

INTERESTING = [
    "/rest/v1.0/me",
    "/rest/v1.0/companies",
    "/rest/v1.0/projects",
    "/rest/v1.0/projects/",  # detail + subresources
    "/rest/v1.0/navigation",
    "/rfis",
    "/submittals",
    "/vendors",
    "/observations/items",
    "/project_memberships",
]

AUTH_HEADER_HINTS = (
    "authorization", "cookie", "x-csrf", "procore", "x-requested-with",
    "content-type", "accept",
)


def shape(obj, depth=0):
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in list(obj.items())[:12]} if depth < 2 else "{...}"
    if isinstance(obj, list):
        inner = shape(obj[0], depth + 1) if obj else None
        return [f"len={len(obj)}", inner]
    return type(obj).__name__


seen_patterns: set[str] = set()
header_name_union: set[str] = set()

for line in CAPTURE.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    rec = json.loads(line)
    url = rec["url"]
    path = urlparse(url).path
    if not any(tok in path for tok in INTERESTING):
        continue

    # Record which request headers exist (names only).
    for h in rec.get("request_headers", {}):
        header_name_union.add(h.lower())

    # Only show each (method, path-with-query-keys) once.
    qs_keys = tuple(sorted(parse_qs(urlparse(url).query).keys()))
    sig = f"{rec['method']} {path} ? {qs_keys}"
    if sig in seen_patterns:
        continue
    seen_patterns.add(sig)

    print("=" * 80)
    print(f"{rec['method']} {url[:200]}")
    print(f"  status: {rec['status']}")
    qs = parse_qs(urlparse(url).query)
    if qs:
        print(f"  query params: {json.dumps(qs)[:300]}")
    interesting_headers = {
        k: v for k, v in rec.get("request_headers", {}).items()
        if any(hint in k.lower() for hint in AUTH_HEADER_HINTS)
    }
    if interesting_headers:
        print(f"  key req headers: {json.dumps(interesting_headers)[:400]}")
    if "response_body_sample" in rec:
        try:
            body = json.loads(rec["response_body_sample"])
            print(f"  response shape: {json.dumps(shape(body))[:500]}")
        except Exception:
            print("  response: (non-JSON or truncated sample)")

print("\n" + "=" * 80)
print("ALL request header names seen on these endpoints:")
print("  " + ", ".join(sorted(header_name_union)))
