"""
Summarize a capture-*.jsonl file into a de-duplicated endpoint catalog.

Run this yourself after a capture to sanity-check coverage before sharing the
file. It collapses numeric ids in URLs (/projects/12345 -> /projects/{id}) so
repeated calls group into one endpoint pattern.

Usage:
    python -m procli.analyze                       # newest capture
    python -m procli.analyze captures/capture-X.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
CAPTURE_DIR = ROOT / "captures"

_ID = re.compile(r"/\d+")
_UUID = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _pattern(url: str) -> str:
    path = urlparse(url).path
    path = _UUID.sub("/{uuid}", path)
    path = _ID.sub("/{id}", path)
    return path


def _newest_capture() -> Path | None:
    files = sorted(CAPTURE_DIR.glob("capture-*.jsonl"))
    return files[-1] if files else None


def run(path: Path) -> None:
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "statuses": set(), "hosts": set(), "example": ""}
    )
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        total += 1
        key = (rec["method"], _pattern(rec["url"]))
        g = groups[key]
        g["count"] += 1
        g["statuses"].add(rec.get("status"))
        g["hosts"].add(urlparse(rec["url"]).hostname or "")
        if not g["example"]:
            g["example"] = rec["url"]

    print(f"\n{path.name}: {total} calls, {len(groups)} unique endpoints\n")
    for (method, pat), g in sorted(groups.items(), key=lambda kv: kv[0][1]):
        statuses = ",".join(str(s) for s in sorted(g["statuses"]))
        print(f"  {method:6} {pat:<55} x{g['count']:<4} [{statuses}]")
    hosts = {h for g in groups.values() for h in g["hosts"]}
    print(f"\nhosts seen: {', '.join(sorted(hosts))}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else _newest_capture()
    if not target or not target.exists():
        sys.exit("No capture file found. Run `python -m procli.capture` first.")
    run(target)
