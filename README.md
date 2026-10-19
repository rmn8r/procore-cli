# procore-cli

A command-line client **+ MCP server** for **Procore** that lets Claude (Cowork /
Claude Code) read your Procore data — RFIs, submittals, observations, inspections,
correspondence, documents, directory, and 360/Enhanced Reports. It works by reusing
**your own** logged-in browser session (no developer app / OAuth), so you only have
the access your Procore login already has.

> **Your session stays on your machine.** Logging in saves `storage_state.json`
> locally (git-ignored); it is never committed or shared. Each user logs in with
> their own account. The regional shard (us02, eu1, …) is auto-detected from your
> session (override with `PROCORE_HOST`).

## Requirements
- **Python 3.10+** and **git** on PATH.
- **Claude Desktop / Cowork** (the MCP registers into its config). Claude Code (CLI) also works.
- Windows or macOS (Linux best-effort). ~400 MB for the bundled Chromium.

## Install into Claude Cowork (from the ZIP — the usual way)
You were handed **`procore-cli.zip`**. In your Cowork session:

1. **Extract** it into your Cowork working folder → you get a `procore-cli/` folder.
2. From inside it: `python install.py`  (creates `.venv`, installs deps + Chromium, ~a few min).
3. **Log in** — a browser opens and **you** sign in to Procore via SSO (Claude can't do this step).
   Run the exact command `install.py` printed, e.g.:
   - Windows: `.venv\Scripts\python.exe -m procli.cli login`
   - macOS/Linux: `.venv/bin/python -m procli.cli login`
4. **Register** the MCP server: `python register_mcp.py` — then **fully quit and reopen
   Claude Desktop/Cowork** (tray/menubar included). The `procore` tools now appear
   (`mcp__procore__*`).

Remove later with `python register_mcp.py --remove`. Your login stays on your machine
(`storage_state.json`, never shared); the regional shard is auto-detected.

## Alternative: install from a git clone
If you have repo access instead of the ZIP:

```bash
git clone https://github.com/rmn8r/procore-cli.git procore-cli
cd procore-cli
python install.py                 # creates .venv, installs deps + Chromium (~a few min)
```
Then **log in** (a browser opens — *the human* signs in to Procore via SSO; Claude can't):
```bash
# use the exact path install.py printed, e.g.:
.venv/bin/python -m procli.cli login            # macOS/Linux
.venv\Scripts\python.exe -m procli.cli login    # Windows
```
Then **register the MCP server** and reload the app:
```bash
python register_mcp.py            # arms a watcher; follow its printed instructions
```
`register_mcp.py` can't edit the Desktop config while the app is running (it gets
reverted), so it waits for you to **fully quit Claude Desktop/Cowork** (tray/menubar
included), patches the config on exit, then you **reopen** it — the `procore` tools
appear. (If the app is already closed: `python register_mcp.py --now`.)

To unregister later: `python register_mcp.py --remove`.

## How it works

1. **Login + discovery** (`procli.capture`): a real Chromium window opens; you
   log in by hand (SSO/MFA and all). While you browse, every API call the site
   makes is recorded, and your session is saved to `storage_state.json`.
2. **Analyze** (`procli.analyze`): summarize the captured calls into an endpoint
   catalog so we know what the CLI can call.
3. **Client + CLI** (`procli.cli`): plain `httpx` calls that reuse the saved
   cookies — fast, no browser at runtime. Re-run capture when the session expires.

## Using the CLI

`app.procore.com` redirects to a regional shard; e.g.
`us02.procore.com` (set in `session.py`). Auth = the saved `_session_id` cookie;
the `csrf_token` cookie is echoed as `x-csrf-token` for writes.

```powershell
.\procore.ps1 companies --table
.\procore.ps1 projects -c "Your Company" --table
.\procore.ps1 rfis -c "Your Company" -p "Your Project" --table
.\procore.ps1 rfi 212 -c "Your Company" -p "Your Project" --table
.\procore.ps1 submittals -c "Your Company" -p <project> --table
.\procore.ps1 raw GET /rest/v1.0/me
```

- Output is **JSON by default** (clean stdout, pipe/parse-friendly); add
  `--table`/`-t` for a human view on list commands.
- `--company`/`-c` and `--project`/`-p` accept an **id or a name substring**.
- `rfi` takes the **display number** (`212` or `RFI-212`); `--table` renders a
  readable view (question + responses + attachments). Use `--by-id` for the
  internal id instead.
- Set `PROCORE_DEFAULT_COMPANY` to skip `-c` every time.
- `raw METHOD PATH [-P key=value ...]` hits any endpoint we discovered — the
  full catalog is in `python -m procli.analyze`.

### Commands
`me`, `companies`, `projects`, `project`, `rfis`, `rfi`, `submittals`,
`submittal`, `corr-types`, `correspondence`, `corr-item`, `observations`,
`vendors`, `members`, `raw`.

- `rfi <num>` / `submittal <num>` take the **display number** (e.g. `212`,
  `RFI-212`, or a submittal's `323119.53-1`); `--table` renders a readable view
  (RFI: question + responses; submittal: status/spec + full approval log).
- **Correspondence** is multi-type. List types with `corr-types`, then:
  ```powershell
  .\procore.ps1 correspondence -c "Your Company" -p <proj> -T "Project Bulletin" --table
  .\procore.ps1 corr-item PB-42 -c "Your Company" -p <proj> -T "Project Bulletin" --table
  ```
  `corr-item` looks up by the item's display position (`PB-42`).

Writes (create/update) are not implemented yet — reads/search/detail first.

**Session note:** Procore has a short idle timeout. Account-level calls (`me`,
`companies`) keep working after project-scoped calls start returning 401
`{"message":"You'll need to login first."}` — that 401 means the session lapsed;
re-run `python -m procli.capture`.

## Re-discovering endpoints

`procli.capture` (login + record), `procli.analyze` (endpoint catalog),
`procli.inspect_capture` / `procli.inspect_auth` (mechanics: params, headers,
cookies), `procli.probe` (live endpoint probing). Use these to add new tools.

## Setup

```bash
cd "procore-cli"
python -m venv .venv
.venv\Scripts\activate            # Windows PowerShell
pip install -r requirements.txt
python -m playwright install chromium
```

## Phase 1: capture

```bash
python -m procli.capture
```

Log in, then visit every screen you want in the CLI (Projects, RFIs, Submittals,
Drawings, Daily Log, Directory, Budget…). Open a couple of individual records too
so we capture both list and detail endpoints. Press ENTER in the terminal to save.

Then summarize what was caught:

```bash
python -m procli.analyze
```

## Security notes

- Your credentials are never seen by this tool or by Claude — you type them into
  the real browser.
- `storage_state.json` holds your live session — treat it like a password. It is
  git-ignored; never share it.
- Captures may contain project data. `procli.capture` masks auth header *values*,
  but review a capture before sharing it.
- This uses Procore's internal endpoints, which can change without notice and may
  be subject to Procore's terms of service. Use with your own account only.
