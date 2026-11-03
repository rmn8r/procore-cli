"""
`procore` command-line interface.

JSON-first by default (so Claude can parse results); pass --table for a
human-readable view on list commands. Companies and projects can be referenced
by id or by case-insensitive name substring.

Examples:
    python -m procli.cli me
    python -m procli.cli companies --table
    python -m procli.cli projects -c "Your Company" --table
    python -m procli.cli rfis -c "Your Company" -p "Springfield" --table
    python -m procli.cli rfi -c "Your Company" -p "Springfield" --id 42
    python -m procli.cli raw GET /rest/v1.0/me
"""

from __future__ import annotations

import json
import os
import sys

import typer
from rich.console import Console
from rich.table import Table

from procli.client import ProcoreClient
from procli.session import ProcoreAuthError, ProcoreSafetyError

app = typer.Typer(add_completion=False, help="Procore CLI (internal-API client).")
console = Console()
err = Console(stderr=True)

DEFAULT_COMPANY = os.environ.get("PROCORE_DEFAULT_COMPANY")


def _emit(data, table: bool, columns: list[str] | None = None):
    """Print JSON (default) or a rich table when --table and columns are given."""
    if table and columns and isinstance(data, list):
        t = Table(show_lines=False, header_style="bold cyan")
        for col in columns:
            t.add_column(col)
        for row in data:
            t.add_row(*[_cell(row, col) for col in columns])
        console.print(t)
    else:
        # Plain stdout (no rich BOM/ANSI) so output pipes and parses cleanly.
        print(json.dumps(data, default=str, indent=2, ensure_ascii=False))


def _cell(row: dict, col: str) -> str:
    val = row
    for part in col.split("."):
        val = val.get(part) if isinstance(val, dict) else None
        if val is None:
            break
    if val is None:
        return ""
    # Procore returns many fields as lists of {id, name, ...} (assignees etc.).
    if isinstance(val, list):
        names = [x.get("name") for x in val if isinstance(x, dict) and x.get("name")]
        return ", ".join(names) if names else f"[{len(val)}]"
    if isinstance(val, dict):
        return str(val.get("name") or val.get("id") or val)
    return str(val)


def _name(x):
    return (x or {}).get("name") if isinstance(x, dict) else x


def _html_to_text(s) -> str:
    """Strip HTML tags and decode entities (&nbsp;, &rsquo;, ...) to plain text."""
    import html
    import re

    if not s:
        return ""
    t = str(s)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|li)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _render_rfi(d: dict):
    """Human-readable single-RFI view for --table."""
    from rich.markup import escape

    def line(label, value):
        console.print(f"[bold]{label}:[/bold] {escape(str(value)) if value not in (None, '') else '—'}")

    console.print(f"[bold cyan]{escape(str(d.get('full_number') or ''))}[/bold cyan]  "
                  f"{escape(str(d.get('subject') or ''))}")
    line("Status", d.get("translated_status") or d.get("status"))
    line("Due", d.get("due_date"))
    line("Created", f"{d.get('created_at')} by {_name(d.get('created_by'))}")
    line("RFI Manager", _name(d.get("rfi_manager")))
    line("Received from", _name(d.get("received_from")))
    ci = d.get("cost_impact") or {}
    si = d.get("schedule_impact") or {}
    line("Cost impact", ci.get("status"))
    line("Schedule impact", si.get("status"))
    line("Assignees", ", ".join(a.get("name", "") for a in (d.get("assignees") or [])))

    q = d.get("question") or {}
    console.print("\n[bold]Question:[/bold]")
    console.print("  " + escape((q.get("plain_text_body") or q.get("body") or "—").strip()))

    if d.get("proposed_solution"):
        console.print("\n[bold]Proposed solution:[/bold]")
        console.print("  " + escape(str(d["proposed_solution"]).strip()))

    resp = d.get("responses") or []
    console.print(f"\n[bold]Responses ({len(resp)}):[/bold]")
    if not resp:
        console.print("  (none yet)")
    for r in resp:
        official = " [green](official)[/green]" if r.get("official") else ""
        console.print(f"  • [bold]{escape(str(_name(r.get('created_by')) or '?'))}[/bold] "
                      f"— {r.get('created_at')}{official}")
        body = (r.get("plain_text_body") or r.get("body") or "").strip()
        if body:
            console.print("    " + escape(body))
        for a in (r.get("attachments") or []):
            console.print(f"    [dim]attachment:[/dim] {escape(str(a.get('filename') or ''))}")


def _first(d: dict, *keys):
    """Return the first present, non-empty value among keys (dot-paths ok)."""
    for k in keys:
        v = d
        for part in k.split("."):
            v = v.get(part) if isinstance(v, dict) else None
            if v is None:
                break
        if v not in (None, "", [], {}):
            return v
    return None


def _render_submittal(d: dict, approvers: dict | None):
    """Human-readable single-submittal view for --table."""
    from rich.markup import escape

    def line(label, value):
        console.print(f"[bold]{label}:[/bold] {escape(str(value)) if value not in (None, '') else '—'}")

    num = _first(d, "formatted_number", "number")
    rev = _first(d, "revision")
    title = _first(d, "title") or ""
    header = f"{num or ''}" + (f" (Rev {rev})" if rev not in (None, "", "0") else "")
    console.print(f"[bold cyan]{escape(str(header))}[/bold cyan]  {escape(str(title))}")

    line("Status", _name(_first(d, "status")))
    spec = _first(d, "specification_section")
    if isinstance(spec, dict):
        spec = " ".join(str(x) for x in (spec.get("number") or spec.get("formatted_number"),
                                         spec.get("description")) if x)
    line("Spec section", spec)
    line("Type", _name(_first(d, "type")))
    line("Submittal manager", _name(_first(d, "submittal_manager")))
    line("Received from", _name(_first(d, "received_from")))
    line("Due", _first(d, "due_date"))
    line("Required on site", _first(d, "required_on_site_date"))
    bic = _first(d, "ball_in_court")
    if isinstance(bic, list):
        bic = ", ".join(x.get("name", "") for x in bic if isinstance(x, dict))
    line("Ball in court", bic)

    desc = _html_to_text(_first(d, "rich_text_description", "description"))
    if desc:
        console.print("\n[bold]Description:[/bold]")
        console.print("  " + escape(desc))

    # Approval workflow: each approver's decision links into responses[] by id.
    appr, rmap = [], {}
    if isinstance(approvers, dict):
        appr = approvers.get("approvers") or []
        rmap = {r["id"]: r for r in (approvers.get("responses") or [])
                if isinstance(r, dict) and "id" in r}
    console.print(f"\n[bold]Approvers ({len(appr)}):[/bold]")
    if not appr:
        console.print("  (none)")
    for a in appr:
        who = a.get("name") or "?"
        comp = a.get("company_name")
        decision = (rmap.get(a.get("response_id")) or {}).get("name") or "Pending"
        tags = []
        if a.get("is_required"):
            tags.append("required")
        if a.get("is_ball_in_court"):
            tags.append("ball-in-court")
        tag_str = f"  ({', '.join(tags)})" if tags else ""
        who_line = f"{who} ({comp})" if comp else who
        console.print(f"  • [bold]{escape(who_line)}[/bold] — {escape(decision)}{escape(tag_str)}")
        dates = []
        for label, key in (("sent", "sent_date"), ("returned", "returned_date"), ("due", "due_date")):
            if a.get(key):
                dates.append(f"{label} {a[key]}")
        if dates:
            console.print("    [dim]" + escape(", ".join(dates)) + "[/dim]")
        ctext = _html_to_text(a.get("comment"))
        if ctext:
            console.print("    " + escape(f"“{ctext}”"))


def _render_correspondence(d: dict):
    """Human-readable single correspondence-item view for --table."""
    from rich.markup import escape

    def line(label, value):
        console.print(f"[bold]{label}:[/bold] {escape(str(value)) if value not in (None, '') else '—'}")

    tool = d.get("generic_tool") or {}
    pos = _first(d, "position")
    title = _first(d, "title") or ""
    console.print(f"[bold cyan]{escape(str(pos or ''))}[/bold cyan]  {escape(str(title))}")
    line("Type", tool.get("title"))
    line("Status", _name(_first(d, "status")) or _first(d, "formatted_status", "status"))
    line("Created", f"{d.get('created_at')} by {_name(d.get('created_by'))}")
    line("Received from", _name(_first(d, "received_from")))
    line("Responded by", _name(_first(d, "responded_by")))
    line("Due", _first(d, "due_date"))
    line("Issued", _first(d, "issued_at"))
    assignees = d.get("assignees") or []
    if isinstance(assignees, list):
        line("Assignees", ", ".join(a.get("name", "") for a in assignees if isinstance(a, dict)))

    desc = _html_to_text(_first(d, "description"))
    if desc:
        console.print("\n[bold]Description:[/bold]")
        console.print("  " + escape(desc))

    atts = d.get("attachments") or []
    if atts:
        console.print(f"\n[bold]Attachments ({len(atts)}):[/bold]")
        for a in atts:
            if isinstance(a, dict):
                console.print(f"  [dim]•[/dim] {escape(str(a.get('filename') or a.get('name') or ''))}")


def _client() -> ProcoreClient:
    try:
        return ProcoreClient()
    except ProcoreAuthError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)


def _resolve_company(c: ProcoreClient, ref: str | None) -> dict:
    ref = ref or DEFAULT_COMPANY
    if not ref:
        err.print("[red]No company given. Use --company NAME|ID or set "
                  "PROCORE_DEFAULT_COMPANY.[/red]")
        raise typer.Exit(2)
    try:
        return c.resolve_company(ref)
    except LookupError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)


def _resolve_project(c: ProcoreClient, company_id: int, ref: str) -> dict:
    try:
        return c.resolve_project(company_id, ref)
    except LookupError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)


# -- options reused across commands -------------------------------------------
CompanyOpt = typer.Option(None, "--company", "-c", help="Company name or id.")
ProjectOpt = typer.Option(..., "--project", "-p", help="Project name or id.")
TableOpt = typer.Option(False, "--table", "-t", help="Human-readable table.")
TypeOpt = typer.Option(..., "--type", "-T", help="Correspondence type name or generic-tool id.")


@app.command()
def login(timeout: int = typer.Option(180, help="Seconds to wait for SSO to complete."),
          fresh: bool = typer.Option(False, "--fresh",
                                     help="Hard logout + full fresh sign-in: drop the saved session "
                                          "first so this does NOT short-circuit as a no-op refresh. "
                                          "Use when a plain login keeps reporting 'refreshed' but "
                                          "calls still come back 'session expired'.")):
    """Open a browser to (re)authenticate; auto-closes once the session is live.

    Plain `login` gently refreshes and returns immediately if the session still
    looks valid. `--fresh` first clears the saved session so a brand-new SSO runs
    — the way out of a stuck session that refresh keeps declaring healthy."""
    from procli.auth import refresh_session, clear_session
    if fresh:
        had = clear_session()
        err.print(f"[yellow]Logged out ({'cleared saved session' if had else 'no saved session'}); "
                  "opening a browser for a fresh sign-in...[/yellow]")
    else:
        err.print("[yellow]Opening a browser to sign in (SSO is often automatic)...[/yellow]")
    if refresh_session(timeout_s=timeout):
        console.print("[green]Session refreshed." + (" (fresh login)" if fresh else "") + "[/green]")
    else:
        err.print("[red]Session not refreshed (timed out or cancelled).[/red]")
        raise typer.Exit(2)


@app.command()
def logout():
    """Hard logout: clear the saved Procore session so the next call forces a fresh
    sign-in. Use this (then `login`) to break out of a stuck session that keeps
    reporting 'refreshed' while tool calls still fail. Local only — never touches
    anything in Procore."""
    from procli.auth import clear_session
    had = clear_session()
    if had:
        console.print("[green]Logged out — saved session cleared. Run `login` (or `login --fresh`) "
                      "to sign in again.[/green]")
    else:
        console.print("[yellow]No saved session to clear. Run `login` to sign in.[/yellow]")


@app.command()
def me(table: bool = TableOpt):
    """Show the current user."""
    with _client() as c:
        _emit(c.me(), table, ["id", "login", "name"])


@app.command()
def companies(table: bool = TableOpt):
    """List companies you can access."""
    with _client() as c:
        _emit(c.companies(), table, ["id", "name", "is_active"])


@app.command()
def projects(company: str = CompanyOpt, table: bool = TableOpt,
             detailed: bool = typer.Option(False, help="Full project records.")):
    """List projects in a company."""
    with _client() as c:
        comp = _resolve_company(c, company)
        data = c.projects(comp["id"], compact=not detailed)
        cols = (["id", "name", "stage_name", "status_name", "open_items"]
                if not detailed else ["id", "name", "city", "active"])
        _emit(data, table, cols)


@app.command()
def project(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt):
    """Show one project's details."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        _emit(c.project(comp["id"], p["id"]), table)


@app.command()
def rfis(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt,
         per_page: int = 150, page: int = 1):
    """List RFIs on a project."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        data = c.rfis(p["id"], comp["id"], page=page, per_page=per_page)
        _emit(data, table, ["full_number", "subject", "status", "due_date", "assignees"])


@app.command()
def rfi(number: str, company: str = CompanyOpt, project: str = ProjectOpt,
        table: bool = TableOpt,
        by_id: bool = typer.Option(False, "--by-id",
                                   help="Treat NUMBER as the internal RFI id, not the display number.")):
    """Show one RFI in detail, by display number (e.g. 212 or RFI-212)."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        if by_id:
            rfi_id = int(number)
        else:
            hit = c.find_rfi_by_number(p["id"], comp["id"], number)
            if not hit:
                err.print(f"[red]No RFI {number!r} found on {p.get('name')!r}.[/red]")
                raise typer.Exit(1)
            rfi_id = hit["id"]
        data = c.rfi(p["id"], rfi_id, comp["id"])
        if table:
            _render_rfi(data)
        else:
            print(json.dumps(data, default=str, indent=2, ensure_ascii=False))


@app.command()
def submittals(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt,
               per_page: int = 150, page: int = 1):
    """List submittals on a project."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        data = c.submittals(p["id"], comp["id"], page=page, per_page=per_page)
        _emit(data, table, ["id", "number", "title", "status.name"])


@app.command()
def submittal(number: str, company: str = CompanyOpt, project: str = ProjectOpt,
              table: bool = TableOpt,
              by_id: bool = typer.Option(False, "--by-id",
                                         help="Treat NUMBER as the internal submittal id.")):
    """Show one submittal in detail, by display number (e.g. 001 or '26 05 00-001')."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        if by_id:
            sub_id = int(number)
        else:
            hit = c.find_submittal_by_number(p["id"], comp["id"], number)
            if not hit:
                err.print(f"[red]No submittal {number!r} found on {p.get('name')!r}.[/red]")
                raise typer.Exit(1)
            sub_id = hit["id"]
        data = c.submittal(p["id"], sub_id, comp["id"])
        if table:
            try:
                approvers = c.submittal_approvers(p["id"], sub_id, comp["id"])
            except Exception:
                approvers = None
            _render_submittal(data, approvers)
        else:
            print(json.dumps(data, default=str, indent=2, ensure_ascii=False))


observations_app = typer.Typer(help="Observations tools.", no_args_is_help=True)
app.add_typer(observations_app, name="observations")


def _render_observation(d: dict, logs=None):
    from rich.markup import escape
    from procli.observations import _cf_value as _ocf

    def line(label, value):
        console.print(f"[bold]{label}:[/bold] {escape(str(value)) if value not in (None, '') else '—'}")

    console.print(f"[bold cyan]#{escape(str(d.get('number') or ''))}[/bold cyan]  {escape(str(d.get('name') or ''))}")
    line("Status", d.get("status"))
    line("Priority", d.get("priority"))
    line("Type", _name(d.get("type")))
    line("Trade", _name(d.get("trade")))
    line("Location", _name(d.get("location")))
    cf = d.get("custom_fields") or {}
    line("Equipment", _ocf(cf, "custom_field_12501"))
    line("ROMP", _ocf(cf, "custom_field_12280"))
    a = d.get("assignees") or ([d["assignee"]] if d.get("assignee") else [])
    line("Assignees", ", ".join(x.get("name", "") for x in a if isinstance(x, dict)))
    line("Created", f"{(d.get('created_at') or '')[:10]} by {_name(d.get('created_by'))}")
    line("Due", d.get("due_date"))
    line("Closed", (d.get("closed_at") or "")[:10])
    desc = _html_to_text(d.get("description_rich_text") or d.get("description"))
    if desc:
        console.print("\n[bold]Description:[/bold]")
        console.print("  " + escape(desc))
    atts = d.get("attachments") or []
    if atts:
        console.print(f"\n[bold]Attachments ({len(atts)}):[/bold]")
        for x in atts:
            if isinstance(x, dict):
                console.print("  [dim]•[/dim] " + escape(str(x.get("filename") or x.get("name") or "")))
    if logs:
        console.print(f"\n[bold]Response log ({len(logs)}):[/bold]")
        for lg in logs:
            who = _name(lg.get("created_by"))
            when = (lg.get("created_at") or "")[:16]
            st = lg.get("status_name") or lg.get("status") or ""
            console.print(f"  • [bold]{escape(str(who or '?'))}[/bold] — {when} {escape(str(st))}")
            body = _html_to_text(lg.get("comment") or lg.get("body") or lg.get("description") or "")
            if body:
                console.print("    " + escape(body))


@observations_app.command("list")
def observations_list(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt,
                      status: str = typer.Option(None, "--status",
                                                 help="Filter status (comma-sep): initiated, ready_for_review, not_accepted, closed."),
                      romp: str = typer.Option(None, "--romp", help="Filter to one ROMP."),
                      type_: str = typer.Option(None, "--type", help="Filter by observation type (substring).")):
    """List observations (filter by status / romp / type)."""
    from procli.observations import list_rollup
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        stat = [s.strip() for s in status.split(",") if s.strip()] if status else None
        data = list_rollup(c, p["id"], comp["id"], status=stat, romp=romp, type_=type_)
        _emit(data, table, ["number", "name", "type", "status", "priority", "romp", "equipment", "assignees"])


@observations_app.command("show")
def observations_show(number: str, company: str = CompanyOpt, project: str = ProjectOpt,
                      table: bool = TableOpt,
                      by_id: bool = typer.Option(False, "--by-id", help="Treat NUMBER as the internal id."),
                      logs: bool = typer.Option(True, "--logs/--no-logs", help="Include the response log.")):
    """Show one observation by display number (or --by-id), with its response log."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        if by_id:
            oid = int(number)
        else:
            hit = c.find_observation_by_number(p["id"], comp["id"], number)
            if not hit:
                err.print(f"[red]No observation #{number} on {p.get('name')!r}.[/red]")
                raise typer.Exit(1)
            oid = hit["id"]
        d = c.observation(oid, p["id"], comp["id"])
        lg = []
        if logs:
            try:
                lg = c.observation_response_logs(oid, p["id"], comp["id"])
                lg = lg if isinstance(lg, list) else (lg.get("data") or [])
            except Exception:
                lg = []
        if table:
            _render_observation(d, lg)
        else:
            out = dict(d)
            out["response_logs"] = lg
            print(json.dumps(out, default=str, indent=2, ensure_ascii=False))


@observations_app.command("export")
def observations_export(out: str = typer.Option(..., "--out", help="Output CSV path."),
                        company: str = CompanyOpt, project: str = ProjectOpt,
                        status: str = typer.Option(None, "--status"),
                        romp: str = typer.Option(None, "--romp"),
                        type_: str = typer.Option(None, "--type")):
    """Export observations to CSV (one row per observation)."""
    from procli.observations import export as obs_export
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        stat = [s.strip() for s in status.split(",") if s.strip()] if status else None
        summary = obs_export(c, p["id"], comp["id"], out, status=stat, romp=romp, type_=type_,
                             log=lambda m: err.print(m))
        print(json.dumps(summary, default=str, indent=2))


@app.command()
def vendors(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt):
    """List vendors/companies in the project directory."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        _emit(c.vendors(p["id"], comp["id"]), table, ["id", "name", "trade"])


@app.command()
def members(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt):
    """List project directory people (memberships)."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        _emit(c.project_members(p["id"], comp["id"]), table)


def _resolve_corr_type(c: ProcoreClient, project_id: int, ref: str) -> dict:
    try:
        return c.resolve_correspondence_type(project_id, ref)
    except LookupError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)


@app.command("corr-types")
def corr_types(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt):
    """List correspondence types (name + generic-tool id) for a project."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        _emit(c.correspondence_types(p["id"]), table, ["generic_tool_id", "name"])


@app.command()
def correspondence(company: str = CompanyOpt, project: str = ProjectOpt,
                   type: str = TypeOpt, table: bool = TableOpt):
    """List correspondence items of a given type (e.g. -T "Project Bulletin")."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        t = _resolve_corr_type(c, p["id"], type)
        data = c.correspondence_items_all(p["id"], comp["id"], t["generic_tool_id"])
        _emit(data, table, ["position", "title", "status", "received_from", "due_date"])


@app.command("corr-item")
def corr_item(number: str, company: str = CompanyOpt, project: str = ProjectOpt,
              type: str = TypeOpt, table: bool = TableOpt):
    """Show one correspondence item by display number (e.g. corr-item PB-42 -T "Project Bulletin")."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        t = _resolve_corr_type(c, p["id"], type)
        hit = c.find_correspondence_by_number(p["id"], comp["id"], t["generic_tool_id"], number)
        if not hit:
            err.print(f"[red]No correspondence item {number!r} in "
                      f"{t['name']!r} on {p.get('name')!r}.[/red]")
            raise typer.Exit(1)
        data = c.correspondence_item(p["id"], comp["id"], t["generic_tool_id"], hit["id"])
        if table:
            _render_correspondence(data)
        else:
            print(json.dumps(data, default=str, indent=2, ensure_ascii=False))


@app.command()
def raw(method: str, path: str,
        company: str = CompanyOpt,
        param: list[str] = typer.Option(None, "--param", "-P",
                                         help="Query param k=v (repeatable).")):
    """Escape hatch: call any endpoint. e.g. raw GET /rest/v1.0/me"""
    params = {}
    for kv in param or []:
        k, _, v = kv.partition("=")
        params[k] = v
    with _client() as c:
        cid = None
        if company:
            cid = _resolve_company(c, company)["id"]
        resp = c.s.request(method.upper(), path, params=params or None, company_id=cid)
        try:
            print(json.dumps(resp.json(), default=str, indent=2, ensure_ascii=False))
        except Exception:
            err.print(f"[yellow]HTTP {resp.status_code}, non-JSON body:[/yellow]")
            console.print(resp.text[:4000])
        if resp.status_code >= 400:
            raise typer.Exit(1)


inspections_app = typer.Typer(help="Inspections (checklist) tools.", no_args_is_help=True)
app.add_typer(inspections_app, name="inspections")


@inspections_app.command("export")
def inspections_export(
    out: str = typer.Option(..., "--out", help="Output CSV path."),
    company: str = CompanyOpt, project: str = ProjectOpt,
    types: str = typer.Option("Commissioning,Quality", "--types",
                              help="Comma-separated inspection types to include (blank = all)."),
    romp: str = typer.Option(None, "--romp", help="Filter to one ROMP, e.g. ROMP01."),
    limit: int = typer.Option(None, "--limit", help="Max rows (cube) / max lists (perlist) — for a quick test."),
    workers: int = typer.Option(10, "--workers", help="Concurrent per-list fetchers (perlist engine)."),
    all_items: bool = typer.Option(False, "--all-items",
                                   help="Include non-respondable items too (default: respondable only)."),
    engine: str = typer.Option("cube", "--engine",
                               help="'cube' = fast Enhanced-Reports bulk API (default); 'perlist' = per-list fetch fallback."),
):
    """Export inspection line items (one row per item) to CSV in the Cx/QC rollup schema."""
    from procli.inspections import export_items, export_items_cube
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        tset = tuple(t.strip() for t in types.split(",") if t.strip()) if types else None
        if engine == "cube":
            summary = export_items_cube(c, p["id"], comp["id"], out, types=tset, romp=romp,
                                        respondable_only=not all_items, max_rows=limit,
                                        log=lambda m: err.print(m))
        else:
            summary = export_items(c, p["id"], comp["id"], out, types=tset, romp=romp,
                                   limit=limit, workers=workers, respondable_only=not all_items,
                                   log=lambda m: err.print(m))
        print(json.dumps(summary, default=str, indent=2, ensure_ascii=False))


@inspections_app.command("list")
def inspections_list(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt,
                     types: str = typer.Option("Commissioning,Quality", "--types",
                                               help="Comma-separated inspection types (blank = all)."),
                     romp: str = typer.Option(None, "--romp", help="Filter to one ROMP, e.g. ROMP01.")):
    """Per-inspection completion rollup (fast; from checklist/lists)."""
    from procli.inspections import list_rollup
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        tset = tuple(t.strip() for t in types.split(",") if t.strip()) if types else None
        data = list_rollup(c, p["id"], comp["id"], types=tset, romp=romp)
        _emit(data, table, ["id", "name", "type", "equipment", "romp", "respondable",
                            "answered", "deficient", "pct_complete"])


reports_app = typer.Typer(help="Enhanced Reports (360) tools.", no_args_is_help=True)
app.add_typer(reports_app, name="reports")


@reports_app.command("list")
def reports_list_cmd(company: str = CompanyOpt, project: str = ProjectOpt, table: bool = TableOpt):
    """List the project's saved 360 reports (mine / shared / templates)."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        j = c.reports_list(p["id"], comp["id"])
        rows = []
        for kind in ("myReports", "mySharedReports", "myCustomToolReports", "myTemplateReports"):
            for r in (j.get(kind) or []):
                rows.append({"id": r.get("id"), "name": r.get("name"), "kind": kind,
                             "is_enhanced": r.get("is_enhanced_report")})
        _emit(rows, table, ["id", "name", "kind", "is_enhanced"])


@reports_app.command("create-inspection")
def reports_create_inspection(company: str = CompanyOpt, project: str = ProjectOpt,
                              name: str = typer.Option("Cx-QC Inspection Items + Rollup", "--name"),
                              types: str = typer.Option("Commissioning,Quality", "--types")):
    """Create the 2-tab Inspection Items + Rollup 360 report and verify it persisted."""
    from procli.reports import inspection_report_body
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        tset = tuple(t.strip() for t in types.split(",") if t.strip()) or ("Commissioning", "Quality")
        body = inspection_report_body(comp["id"], name,
                                      "Cx & Quality inspections. Tab 1 item-level; Tab 2 rollup.", tset)
        resp = c.report_create(p["id"], comp["id"], body)
        rid = resp.get("id")
        det = c.report_get(p["id"], comp["id"], rid) if rid else {}
        tabs = det.get("report_tabs_attributes") or []
        cols = [len((t.get("data_set_config") or {}).get("columnState") or []) for t in tabs]
        print(json.dumps({"id": rid, "name": det.get("name"), "tabs": len(tabs),
                          "columns_per_tab": cols, "persisted": bool(cols and all(cols))},
                         indent=2, default=str))


@reports_app.command("delete")
def reports_delete_cmd(report_id: str, company: str = CompanyOpt, project: str = ProjectOpt):
    """Delete a saved report by id."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        st = c.report_delete(p["id"], comp["id"], report_id)
        print(json.dumps({"deleted": report_id, "status": st}))


@reports_app.command("load")
def reports_load_cmd(company: str = CompanyOpt, project: str = ProjectOpt,
                     dimensions: str = typer.Option(..., "--dimensions", help="Comma-separated cube members."),
                     measures: str = typer.Option(None, "--measures", help="Comma-separated measure members."),
                     filter_type: str = typer.Option(None, "--filter-type",
                                                     help="Filter v3_inspections.inspection_type (comma-sep)."),
                     limit: int = typer.Option(100, "--limit"),
                     data_set: str = typer.Option("Project_Management", "--data-set")):
    """Escape hatch: run a raw Enhanced-Reports Cube query and print the rows."""
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        dims = [d.strip() for d in dimensions.split(",") if d.strip()]
        meas = [m.strip() for m in measures.split(",") if m.strip()] if measures else []
        filters = []
        if filter_type:
            filters = [{"member": "v3_inspections.inspection_type", "operator": "equals",
                        "values": [t.strip() for t in filter_type.split(",")]}]
        j = c.reporting_load(p["id"], comp["id"], dims, measures=meas, filters=filters,
                             limit=limit, data_set=data_set)
        print(json.dumps(j.get("data", j), default=str, indent=2, ensure_ascii=False))


@app.command()
def surface(company: str = CompanyOpt, project: str = ProjectOpt,
            domain: str = typer.Option(None, "--domain",
                                       help="rfis|submittals|observations|inspections|correspondence|documents "
                                            "(omit for the overview map)."),
            status: str = typer.Option(None, "--status"),
            romp: str = typer.Option(None, "--romp"),
            type_: str = typer.Option(None, "--type"),
            folder: str = typer.Option(None, "--folder", help="Documents: filter by path substring."),
            limit: int = typer.Option(1000, "--limit")):
    """Surface map: an overview of every domain (no --domain), or a name/number roster for one domain."""
    from procli import surface as sf
    with _client() as c:
        comp = _resolve_company(c, company)
        p = _resolve_project(c, comp["id"], project)
        if domain:
            out = sf.roster(c, p["id"], comp["id"], domain, status=status, romp=romp,
                            type_=type_, folder=folder, limit=limit)
        else:
            out = {"project": {"id": p["id"], "name": p.get("name")},
                   "domains": sf.overview(c, p["id"], comp["id"])}
        print(json.dumps(out, default=str, indent=2, ensure_ascii=False))


def main():
    # A session that lapses mid-command raises ProcoreAuthError from deep in the
    # request path. Catch it here, open a browser for SSO (usually silent since
    # the Okta cookies persist), then transparently retry the command once.
    # Set PROCORE_NO_AUTO_LOGIN=1 to disable (e.g. for headless/scripted runs).
    try:
        app()
    except ProcoreSafetyError as e:
        err.print(f"[red]{e}[/red]")
        raise SystemExit(3)
    except ProcoreAuthError as e:
        if os.environ.get("PROCORE_NO_AUTO_LOGIN"):
            err.print(f"[red]{e}[/red]")
            raise SystemExit(2)
        err.print("[yellow]Session expired — opening a browser to sign in "
                  "(SSO is often automatic)...[/yellow]")
        try:
            from procli.auth import refresh_session
            refreshed = refresh_session()
        except Exception as ex:
            err.print(f"[red]Auto-login failed: {ex}[/red]")
            raise SystemExit(2)
        if not refreshed:
            err.print("[red]Could not refresh the Procore session (the browser SSO "
                      "didn't complete). Run the `login` tool again (CLI: "
                      "`python -m procli.cli login`) and finish the sign-in, then retry — "
                      "that one session covers every Procore tool.[/red]")
            raise SystemExit(2)
        err.print("[green]Signed in — retrying your command.[/green]")
        try:
            app()
        except ProcoreAuthError as e2:
            err.print(f"[red]{e2}[/red]")
            raise SystemExit(2)


if __name__ == "__main__":
    main()
