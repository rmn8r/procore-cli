#!/usr/bin/env python3
"""MCP server exposing the Procore CLI (`procli`) as tools (stdio, JSON-RPC 2.0).

Mirrors the olk-outlook MCP pattern: stdlib-only server that shells out to the
CLI and returns its JSON output, so behaviour is identical to `procore ...`.
Runtime deps (httpx/typer/playwright) live in the procore-cli venv, so this
server MUST be launched with that venv's python (sys.executable is then reused
for the subprocess). Session auto-refresh (SSO) is inherited from the CLI: if the
Procore session has lapsed, a tool call opens a browser to sign in, then retries.

Transport: newline-delimited JSON-RPC 2.0 over stdio. stdout is the protocol
channel only; diagnostics go to stderr.

Run standalone:  <venv>\\Scripts\\python.exe procore_mcp.py
"""
from __future__ import annotations

import sys, os, json, subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))   # procore-cli/ (contains procli/)
PYEXE = sys.executable
SERVER_NAME = "procore"
SERVER_VERSION = "1.0.0"


def log(msg: str) -> None:
    sys.stderr.write(f"[procore-mcp] {msg}\n"); sys.stderr.flush()


def run_cli(argv, timeout=300):
    """Run `python -m procli.cli <argv>`; return (text, is_error).
    Long default timeout so an auto-login browser prompt can complete."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        p = subprocess.run(
            [PYEXE, "-m", "procli.cli"] + [str(a) for a in argv],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        # CLI prints JSON to stdout; errors/status to stderr. Prefer stdout.
        text = out or err or "(no output)"
        if out and err:
            text = out + ("\n\n[stderr]\n" + err if p.returncode != 0 else "")
        return text, (p.returncode != 0)
    except subprocess.TimeoutExpired:
        return (f"procore CLI timed out after {timeout}s (session sign-in may be "
                "pending — complete SSO in the browser and retry)."), True
    except Exception as exc:  # noqa: BLE001
        return f"error running procore CLI: {exc}", True


def build(spec, args):
    """Turn a tool spec + arguments dict into a CLI argv list."""
    args = args or {}
    argv = list(spec["base"])
    for key in spec.get("pos", []):
        v = args.get(key)
        if v not in (None, ""):
            argv.append(str(v))
    pr = spec.get("pos_rep")
    if pr:
        v = args.get(pr, []) or []
        if isinstance(v, str):
            v = [v]
        argv += [str(x) for x in v]
    for key, flag, kind in spec.get("opt", []):
        v = args.get(key)
        if v in (None, "", False):
            continue
        if kind == "bool":
            argv.append(flag)
        else:
            argv += [flag, str(v)]
    for key, flag in spec.get("rep", []):
        v = args.get(key) or []
        if isinstance(v, str):
            v = [v]
        for item in v:
            argv += [flag, str(item)]
    return argv


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_COMPANY = {**_STR, "description": "Company name (substring) or id, e.g. 'Your Company'"}
_PROJECT = {**_STR, "description": "Project name (substring) or id, e.g. 'Your Project'"}


def _t(name, desc, props, required, spec):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props,
                            "required": required, "additionalProperties": False},
            "spec": spec}


TOOLS = [
    _t("me", "Show the current Procore user (id, login, name). JSON.", {}, [],
       {"base": ["me"]}),

    _t("companies", "List companies you can access (id + name). JSON.", {}, [],
       {"base": ["companies"]}),

    _t("projects",
       "List projects in a company. Compact by default (id/name/stage/status/open_items); "
       "set detailed for full records. JSON.",
       {"company": _COMPANY, "detailed": {**_BOOL, "description": "full project records"}},
       ["company"],
       {"base": ["projects"], "opt": [("company", "-c", "val"), ("detailed", "--detailed", "bool")]}),

    _t("project", "Show one project's full details. JSON.",
       {"company": _COMPANY, "project": _PROJECT}, ["company", "project"],
       {"base": ["project"], "opt": [("company", "-c", "val"), ("project", "-p", "val")]}),

    _t("rfis", "List RFIs on a project (number, subject, status, assignees, due). JSON.",
       {"company": _COMPANY, "project": _PROJECT, "per_page": _INT, "page": _INT},
       ["company", "project"],
       {"base": ["rfis"], "opt": [("company", "-c", "val"), ("project", "-p", "val"),
                                  ("per_page", "--per-page", "int"), ("page", "--page", "int")]}),

    _t("rfi", "Show one RFI in detail by display number (e.g. 212 or RFI-212): question, "
              "responses, assignees, impacts. JSON.",
       {"number": {**_STR, "description": "display number, e.g. 212 or RFI-212"},
        "company": _COMPANY, "project": _PROJECT,
        "by_id": {**_BOOL, "description": "treat number as the internal RFI id"}},
       ["number", "company", "project"],
       {"base": ["rfi"], "pos": ["number"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"), ("by_id", "--by-id", "bool")]}),

    _t("submittals", "List submittals on a project (formatted_number, title, status). JSON.",
       {"company": _COMPANY, "project": _PROJECT, "per_page": _INT, "page": _INT},
       ["company", "project"],
       {"base": ["submittals"], "opt": [("company", "-c", "val"), ("project", "-p", "val"),
                                        ("per_page", "--per-page", "int"), ("page", "--page", "int")]}),

    _t("submittal", "Show one submittal in detail by display number (e.g. 323119.53-1): "
                    "status, spec section, and full approval log. JSON.",
       {"number": {**_STR, "description": "display number, e.g. 323119.53-1"},
        "company": _COMPANY, "project": _PROJECT,
        "by_id": {**_BOOL, "description": "treat number as the internal submittal id"}},
       ["number", "company", "project"],
       {"base": ["submittal"], "pos": ["number"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"), ("by_id", "--by-id", "bool")]}),

    _t("corr_types", "List correspondence types (name + generic-tool id) for a project. JSON.",
       {"company": _COMPANY, "project": _PROJECT}, ["company", "project"],
       {"base": ["corr-types"], "opt": [("company", "-c", "val"), ("project", "-p", "val")]}),

    _t("correspondence", "List correspondence items of a type (e.g. type='Project Bulletin'): "
                         "position, title, status, due. JSON.",
       {"company": _COMPANY, "project": _PROJECT,
        "type": {**_STR, "description": "correspondence type name or generic-tool id"}},
       ["company", "project", "type"],
       {"base": ["correspondence"], "opt": [("company", "-c", "val"), ("project", "-p", "val"),
                                            ("type", "-T", "val")]}),

    _t("corr_item", "Show one correspondence item by display position (e.g. PB-42) within a type: "
                    "description, assignees, attachments. JSON.",
       {"number": {**_STR, "description": "item position, e.g. PB-42"},
        "company": _COMPANY, "project": _PROJECT,
        "type": {**_STR, "description": "correspondence type name or generic-tool id"}},
       ["number", "company", "project", "type"],
       {"base": ["corr-item"], "pos": ["number"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"), ("type", "-T", "val")]}),

    _t("observations", "List observations on a project (number, name, type, status, priority, "
                       "ROMP, equipment, assignees). Filter by status/romp/type. JSON.",
       {"company": _COMPANY, "project": _PROJECT,
        "status": {**_STR, "description": "comma-sep: initiated, ready_for_review, not_accepted, closed"},
        "romp": {**_STR, "description": "filter to one ROMP, e.g. ROMP01"},
        "type": {**_STR, "description": "observation type substring, e.g. 'Commissioning Issue'"}},
       ["company", "project"],
       {"base": ["observations", "list"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"),
                ("status", "--status", "val"), ("romp", "--romp", "val"), ("type", "--type", "val")]}),

    _t("observation_show", "Show one observation by display number (or --by-id): status, priority, "
                           "type, trade, ROMP, equipment, assignees, description, attachments, and "
                           "the response log. JSON.",
       {"number": {**_STR, "description": "display number, or internal id with by_id"},
        "company": _COMPANY, "project": _PROJECT,
        "by_id": {**_BOOL, "description": "treat number as the internal observation id"}},
       ["number", "company", "project"],
       {"base": ["observations", "show"], "pos": ["number"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"), ("by_id", "--by-id", "bool")]}),

    _t("observations_export", "Export observations to a CSV at 'out' (one row per observation). "
                              "Filter by status/romp/type. Returns a JSON summary.",
       {"out": {**_STR, "description": "output CSV path"}, "company": _COMPANY, "project": _PROJECT,
        "status": _STR, "romp": _STR, "type": _STR},
       ["out", "company", "project"],
       {"base": ["observations", "export"],
        "opt": [("out", "--out", "val"), ("company", "-c", "val"), ("project", "-p", "val"),
                ("status", "--status", "val"), ("romp", "--romp", "val"), ("type", "--type", "val")]}),

    _t("vendors", "List vendors/companies in the project directory. JSON.",
       {"company": _COMPANY, "project": _PROJECT}, ["company", "project"],
       {"base": ["vendors"], "opt": [("company", "-c", "val"), ("project", "-p", "val")]}),

    _t("members", "List project directory people (memberships). JSON.",
       {"company": _COMPANY, "project": _PROJECT}, ["company", "project"],
       {"base": ["members"], "opt": [("company", "-c", "val"), ("project", "-p", "val")]}),

    _t("login", "Refresh the Procore session. There is ONE shared session behind every "
                "Procore tool (RFIs, submittals, inspections, observations, reports, …); this "
                "refreshes it for all of them (opens a browser; SSO is usually automatic, and "
                "verifies real tool access before returning). Use it if a call reports the "
                "session expired, then simply retry the original call — no manual `capture` "
                "step and no separate per-tool login exists. IMPORTANT: if a plain login keeps "
                "saying 'Session refreshed' but calls STILL come back 'session expired', the "
                "saved session is stuck — call this with fresh=true (a hard logout + brand-new "
                "sign-in) to break out of it, or call the `logout` tool first.",
       {"timeout": {**_INT, "description": "seconds to wait for SSO (default 180)"},
        "fresh": {**_BOOL, "description": "hard logout + full fresh sign-in: clear the saved "
                  "session first so this cannot short-circuit as a no-op refresh. Use when a "
                  "normal login reports success but calls still fail."}}, [],
       {"base": ["login"], "opt": [("timeout", "--timeout", "int"), ("fresh", "--fresh", "bool")]}),

    _t("logout", "Hard logout: clear the saved Procore session so the next call forces a fresh "
                 "sign-in. Use this (then `login`), or `login` with fresh=true, to break out of a "
                 "stuck session that keeps reporting 'refreshed' while tool calls still fail with "
                 "'session expired'. Local only — never deletes or changes anything in Procore.",
       {}, [], {"base": ["logout"]}),

    {**_t("inspections_export",
        "Export inspection line items (one row per checklist item) to a CSV at 'out', in the "
        "Cx/QC rollup schema: Inspection ID, Name, Type, Equipment (cf 12501), ROMP (cf 12280), "
        "Section, Item, Response, Location. Scope with types/romp/limit. Large project-wide runs "
        "take several minutes. Returns a JSON summary (rows, answered, out path).",
        {"out": {**_STR, "description": "output CSV path"},
         "company": _COMPANY, "project": _PROJECT,
         "types": {**_STR, "description": "comma-separated inspection types (default 'Commissioning,Quality'; blank=all)"},
         "romp": {**_STR, "description": "filter to one ROMP, e.g. ROMP01"},
         "limit": {**_INT, "description": "max lists (quick test)"},
         "all_items": {**_BOOL, "description": "include non-respondable items too"}},
        ["out", "company", "project"],
        {"base": ["inspections", "export"],
         "opt": [("out", "--out", "val"), ("company", "-c", "val"), ("project", "-p", "val"),
                 ("types", "--types", "val"), ("romp", "--romp", "val"),
                 ("limit", "--limit", "int"), ("all_items", "--all-items", "bool")]}),
     "timeout_s": 1800},

    _t("inspections_list",
       "Per-inspection completion rollup (fast): one row per inspection with id, name, type, "
       "Equipment, ROMP, location, respondable/answered/deficient counts, % complete. JSON.",
       {"company": _COMPANY, "project": _PROJECT,
        "types": {**_STR, "description": "comma-separated inspection types (default 'Commissioning,Quality')"},
        "romp": {**_STR, "description": "filter to one ROMP, e.g. ROMP01"}},
       ["company", "project"],
       {"base": ["inspections", "list"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"),
                ("types", "--types", "val"), ("romp", "--romp", "val")]}),

    _t("reports_list", "List the project's saved 360 reports (mine / shared / templates). JSON.",
       {"company": _COMPANY, "project": _PROJECT}, ["company", "project"],
       {"base": ["reports", "list"], "opt": [("company", "-c", "val"), ("project", "-p", "val")]}),

    _t("reports_create_inspection",
       "Create the 2-tab 'Inspection Items + Rollup' 360 report in Procore and verify it persisted. "
       "Returns {id, tabs, columns_per_tab, persisted}. Creates an object in the account.",
       {"company": _COMPANY, "project": _PROJECT,
        "name": {**_STR, "description": "report name"},
        "types": {**_STR, "description": "comma-separated inspection types (default 'Commissioning,Quality')"}},
       ["company", "project"],
       {"base": ["reports", "create-inspection"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"),
                ("name", "--name", "val"), ("types", "--types", "val")]}),

    # NOTE: reports_delete is intentionally NOT exposed via MCP — deletes are
    # blocked by the session safety guard (PROCORE_ALLOW_DELETE), so the MCP
    # cannot delete anything in Procore. The `reports delete` CLI command still
    # exists for a human who explicitly opts in.

    _t("reports_load",
       "Escape hatch: run a raw Enhanced-Reports Cube query. dimensions/measures are comma-separated "
       "cube members (e.g. 'v3_inspection_items.section,v3_inspection_items.responded_with'); "
       "filter_type filters inspection_type. JSON rows.",
       {"company": _COMPANY, "project": _PROJECT,
        "dimensions": {**_STR, "description": "comma-separated cube dimension members"},
        "measures": {**_STR, "description": "comma-separated measure members"},
        "filter_type": {**_STR, "description": "inspection types to filter (comma-sep)"},
        "limit": _INT, "data_set": {**_STR, "description": "data set (default Project_Management)"}},
       ["company", "project", "dimensions"],
       {"base": ["reports", "load"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"),
                ("dimensions", "--dimensions", "val"), ("measures", "--measures", "val"),
                ("filter_type", "--filter-type", "val"), ("limit", "--limit", "int"),
                ("data_set", "--data-set", "val")]}),

    _t("surface",
       "Surface map of the project. With NO domain: an overview of every domain (per-domain counts + "
       "breakdowns — RFIs/submittals/observations by status/type, inspections by type/ROMP/template, "
       "correspondence types, document folder tree). With domain set to one of "
       "rfis|submittals|observations|inspections|correspondence|documents: a name/number roster for that "
       "domain (filter with status/romp/type/folder). Call it to see what exists, then drill in with the "
       "typed tools. JSON.",
       {"company": _COMPANY, "project": _PROJECT,
        "domain": {**_STR, "description": "one of rfis|submittals|observations|inspections|correspondence|documents (omit for overview)"},
        "status": _STR, "romp": _STR, "type": _STR,
        "folder": {**_STR, "description": "documents: filter by path substring"},
        "limit": _INT},
       ["company", "project"],
       {"base": ["surface"],
        "opt": [("company", "-c", "val"), ("project", "-p", "val"), ("domain", "--domain", "val"),
                ("status", "--status", "val"), ("romp", "--romp", "val"), ("type", "--type", "val"),
                ("folder", "--folder", "val"), ("limit", "--limit", "int")]}),

    _t("raw", "Escape hatch: call any Procore internal REST endpoint. method e.g. GET, "
              "path e.g. /rest/v1.0/me, params as ['key=value', ...]. JSON.",
       {"method": {**_STR, "description": "HTTP method, e.g. GET"},
        "path": {**_STR, "description": "endpoint path, e.g. /rest/v1.0/me"},
        "company": _COMPANY,
        "params": {"type": "array", "items": _STR, "description": "query params as key=value"}},
       ["method", "path"],
       {"base": ["raw"], "pos": ["method", "path"],
        "opt": [("company", "-c", "val")], "rep": [("params", "-P")]}),
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def public_tools():
    return [{"name": t["name"], "description": t["description"],
             "inputSchema": t["inputSchema"]} for t in TOOLS]


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    is_notification = "id" not in req

    if method == "initialize":
        client_ver = (req.get("params") or {}).get("protocolVersion")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": client_ver or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": public_tools()}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = TOOL_BY_NAME.get(name)
        if not tool:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        try:
            argv = build(tool["spec"], arguments)
            text, is_error = run_cli(argv, timeout=tool.get("timeout_s", 300))
        except Exception as exc:  # noqa: BLE001
            text, is_error = f"tool error: {exc}", True
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error}}

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    log(f"starting {SERVER_NAME} v{SERVER_VERSION}; ROOT={ROOT}; py={PYEXE}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"bad JSON: {exc}")
            continue
        try:
            resp = handle(req)
        except Exception as exc:  # noqa: BLE001
            log(f"handler crash: {exc}")
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
