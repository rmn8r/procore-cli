# Procore MCP server

`procore_mcp.py` exposes the Procore CLI (`procli`) as an MCP server (stdio,
JSON-RPC 2.0), so any Claude session — Claude Code **and Cowork** — can call
Procore as tools. It shells out to `python -m procli.cli`, so behaviour is
identical to the CLI, including **auto session-refresh** (a lapsed session opens
a browser for SSO, then the call retries).

## Tools (24)
`me`, `companies`, `projects`, `project`, `surface`, `rfis`, `rfi`, `submittals`,
`submittal`, `corr_types`, `correspondence`, `corr_item`, `observations`,
`observation_show`, `observations_export`, `vendors`, `members`, `login`,
`inspections_export`, `inspections_list`, `reports_list`,
`reports_create_inspection`, `reports_load`, `raw`. All return JSON.

- `surface` — the index/roster. No `domain` → overview of every domain (counts +
  breakdowns: RFIs/submittals/observations by status/type, inspections by
  type/ROMP/template, correspondence types, document folder tree). `domain=` one of
  rfis|submittals|observations|inspections|correspondence|documents → a name/number
  roster (filter status/romp/type/folder). Orient first, then drill in.
- `observations` — list, filter by status/romp/type. `observation_show <number>`
  — detail + response log. `observations_export` — CSV, one row per observation.

## Safety: no deletes
A transport-layer guard in `session.py` **blocks all DELETE requests** (covers CLI,
MCP, and `raw`) unless `PROCORE_ALLOW_DELETE=1` is set — and the MCP server is
launched without it, so **no Cowork/MCP call can delete anything in Procore**.
`reports_delete` is intentionally not exposed via MCP; the `reports delete` CLI
command remains for a human who sets the override for a single run.

- `inspections_export` — CSV, one row per checklist line item (Inspection ID/Name/
  Type, Equipment=cf 12501, ROMP=cf 12280, Section, Item, Response, Location).
  Scope with types/romp/limit. Cube engine (fast); 1800s timeout.
- `inspections_list` — per-inspection completion rollup (respondable/answered/
  deficient counts + % complete), fast.
- `reports_list` / `reports_create_inspection` / `reports_delete` — manage saved
  Enhanced (360) reports. `create_inspection` builds the 2-tab Inspection Items +
  Rollup report and verifies it persisted.
- `reports_load` — raw Enhanced-Reports Cube query (dimensions/measures/filter_type).

> Adding tools requires a **session restart** to reload the MCP server.

They surface in sessions as `mcp__procore__<tool>` (e.g. `mcp__procore__rfi`).

## Registration
Use the installer — **`python register_mcp.py`** — which computes the absolute paths
for *your* clone (no hardcoded paths) and writes the entry to:
- `%APPDATA%\Claude\claude_desktop_config.json` (Win) / `~/Library/Application Support/Claude/…`
  (macOS) → `mcpServers.procore` — **Claude Desktop / Cowork**.
- `~/.claude.json` → top-level `mcpServers.procore` — **Claude Code (CLI)**.

Backups of each config are written as `*.bak-procore-<timestamp>`. The entry it
writes (paths filled in for your machine, launched with the venv python so
httpx/typer/playwright are available):

```json
"procore": {
  "command": "<clone>/.venv/Scripts/python.exe",
  "args": ["<clone>/procore_mcp.py"],
  "env": {}
}
```
(`type` is omitted in the desktop-config variant.) Backups: `*.bak-procoremcp`.

**MCP servers load at session start — restart Cowork / start a new CLI session
to pick up the tools.**

## Test standalone
```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | .venv/Scripts/python.exe procore_mcp.py
```

## Notes
- Session/auth: the CLI reuses `storage_state.json`. If it has lapsed, a tool
  call (or the `login` tool) opens a browser for SSO. Set `PROCORE_NO_AUTO_LOGIN=1`
  in the entry's `env` to disable auto-login (calls then error instead of prompting).
- Adding a tool: add a `_t(...)` entry in `procore_mcp.py` mapping to a CLI command.
