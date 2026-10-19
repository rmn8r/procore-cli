#!/usr/bin/env python3
"""
One-shot environment setup for the Procore CLI + MCP server.

Creates a local .venv, installs dependencies, and downloads the Playwright
Chromium build. Cross-platform; run with any Python 3.10+:

    python install.py

Then:
    1) log in            (prints the exact command)
    2) python register_mcp.py   (registers the MCP server for Claude Desktop/Cowork)
    3) restart Claude Desktop / Cowork
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_python(v: Path) -> Path:
    return v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd: list) -> None:
    print("+", " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd)


def main() -> None:
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10+ required; found {platform.python_version()}")
    if not VENV.exists():
        print(f"Creating virtual environment at {VENV} ...")
        venv.create(VENV, with_pip=True)
    py = str(venv_python(VENV))
    run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    run([py, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")])
    run([py, "-m", "playwright", "install", "chromium"])
    print("\n" + "=" * 68)
    print("Environment ready. Next steps:")
    print(f"  1) Log in to Procore:   {py} -m procli.cli login")
    print("     (opens a browser; sign in with YOUR Procore account via SSO)")
    print("  2) Register the MCP server:  python register_mcp.py")
    print("  3) Fully quit, then reopen Claude Desktop / Cowork")
    print("=" * 68)


if __name__ == "__main__":
    main()
