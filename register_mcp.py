#!/usr/bin/env python3
"""
Register (or remove) the Procore MCP server in the Claude Desktop / Cowork config
— cross-platform, stdlib only, with no hardcoded paths.

Claude Desktop keeps `claude_desktop_config.json` in memory and rewrites it when
it saves settings, so an edit made while the app is running is silently reverted.
This script therefore, by default, spawns a detached watcher that waits until the
desktop app has fully exited, then patches the file. So the flow is:

    python register_mcp.py      # arms the watcher, prints instructions
    # -> fully QUIT Claude Desktop/Cowork (tray too); it gets registered on exit
    # -> reopen Claude Desktop/Cowork

Options:
    --now        patch immediately (use when the app is already closed)
    --remove     unregister instead of register
    --name NAME  server name (default: procore)
    --watch-exit internal: the detached watcher mode

It also patches ~/.claude.json (Claude Code CLI) immediately, which is safe —
that file is not reverted.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_SCRIPT = ROOT / "procore_mcp.py"


def venv_python() -> Path:
    return ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def desktop_config_path() -> Path:
    sysname = platform.system()
    if sysname == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return base / "Claude" / "claude_desktop_config.json"
    if sysname == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def code_config_path() -> Path:
    return Path.home() / ".claude.json"


def entry(desktop: bool) -> dict:
    e = {"command": str(venv_python()), "args": [str(SERVER_SCRIPT)], "env": {}}
    if not desktop:
        e = {"type": "stdio", **e}  # Claude Code wants "type"
    return e


def _patch(path: Path, name: str, desktop: bool, remove: bool) -> str:
    if not path.exists():
        if remove:
            return f"{path.name}: not found, nothing to remove"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return f"{path.name}: unreadable ({exc}); skipped"
    servers = data.setdefault("mcpServers", {})
    if remove:
        if name not in servers:
            return f"{path.name}: '{name}' not present"
        servers.pop(name)
    else:
        servers[name] = entry(desktop)
    if path.exists():
        backup = path.with_suffix(path.suffix + f".bak-procore-{time.strftime('%Y%m%d%H%M%S')}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    verb = "removed" if remove else "registered"
    return f"{path.name}: {verb} '{name}' (servers now: {', '.join(servers) or 'none'})"


def desktop_app_running() -> bool:
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe"],
                                 capture_output=True, text=True).stdout
            return "Claude.exe" in out
        if platform.system() == "Darwin":
            return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True).returncode == 0
        return subprocess.run(["pgrep", "-f", "claude"], capture_output=True).returncode == 0
    except Exception:
        return False


def watch_then_patch(name: str, remove: bool, timeout_min: int = 30) -> None:
    """Wait for the desktop app to fully exit, then patch its config."""
    cfg = desktop_config_path()
    deadline = time.time() + timeout_min * 60
    gone = 0
    while time.time() < deadline:
        gone = gone + 1 if not desktop_app_running() else 0
        if gone >= 3:  # absent for ~3s
            break
        time.sleep(1)
    msg = _patch(cfg, name, desktop=True, remove=remove)
    (ROOT / "register_mcp.log").open("a", encoding="utf-8").write(
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="patch desktop config immediately")
    ap.add_argument("--remove", action="store_true", help="unregister instead of register")
    ap.add_argument("--name", default="procore")
    ap.add_argument("--watch-exit", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.watch_exit:
        watch_then_patch(args.name, args.remove)
        return

    if not SERVER_SCRIPT.exists():
        sys.exit(f"procore_mcp.py not found at {SERVER_SCRIPT}")
    if not venv_python().exists():
        sys.exit("No .venv yet — run `python install.py` first.")

    # 1) Claude Code CLI config — safe to patch immediately.
    print(_patch(code_config_path(), args.name, desktop=False, remove=args.remove))

    # 2) Desktop/Cowork config.
    cfg = desktop_config_path()
    if args.now or not desktop_app_running():
        print(_patch(cfg, args.name, desktop=True, remove=args.remove))
        print("\nDone. If Claude Desktop/Cowork is open, fully quit and reopen it to load the change.")
    else:
        flags = {}
        if os.name == "nt":
            flags["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            flags["start_new_session"] = True
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                          "--watch-exit", "--name", args.name] + (["--remove"] if args.remove else []),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
        print("\nClaude Desktop/Cowork is running, which would revert a direct edit.")
        print(">>> FULLY QUIT Claude Desktop/Cowork now (including the tray/menubar icon).")
        print("    A background watcher will register the server the moment it exits;")
        print("    then reopen Claude Desktop/Cowork and the 'procore' tools appear.")
        print("    (Or quit it and run `python register_mcp.py --now`.)")


if __name__ == "__main__":
    main()
