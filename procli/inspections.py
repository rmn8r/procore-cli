"""
Inspection item-level export for the Procore CLI.

Procore's Inspections tool = checklist lists. There is no bulk item endpoint, so
this pulls list-level attributes in one sweep (`checklist/lists`, which DOES carry
custom fields) and then fetches items per list (`list_items` + `list_sections`),
emitting one row per respondable line item to the schema the Cx/QC rollup expects:

  Inspection ID, Inspection Name, Inspection Type, Equipment, ROMP,
  Section, Item, Response, Location

Equipment = custom field 12501 (falls back to parsing Description).
ROMP       = custom field 12280 (falls back to parsing Description).
Response   = item `responded_with`; "answered" = Response != "No Response".
"""

from __future__ import annotations

import csv
import re
import threading
from concurrent.futures import ThreadPoolExecutor

CF_EQUIPMENT = "custom_field_12501"
CF_ROMP = "custom_field_12280"

COLUMNS = ["Inspection ID", "Inspection Name", "Inspection Type", "Equipment",
           "ROMP", "Section", "Item", "Response", "Location"]

# Types considered in-scope for the report (IPQ is type "Quality", so it is
# included here by design — filter IPQ-* downstream).
DEFAULT_TYPES = ("Commissioning", "Quality")

_ROMP_RE = re.compile(r"ROMP\s*0*\d+(?:\.\d+)?", re.I)
# {projectcode}ROMP##.### — projectcode varies by job (PHX069, PHX070, …).
_PREFIX_ROMP_RE = re.compile(r"[A-Za-z0-9]*ROMP\s*0*\d+(?:\.\d+)?", re.I)


def _cf_value(cf: dict, key: str):
    """Extract a custom field's display value (string or lov label)."""
    ent = (cf or {}).get(key) or {}
    val = ent.get("value")
    if val is None:
        return None
    if isinstance(val, list):  # lov_entries -> [{id,label}]
        labels = [x.get("label") for x in val if isinstance(x, dict) and x.get("label")]
        return ", ".join(labels) if labels else None
    return str(val).strip() or None


def parse_equipment_romp(description: str, template: str) -> tuple[str | None, str | None]:
    """Fallback: derive (equipment, romp) from the description naming convention
    `{EQUIP-TAG}-{template}-{PROJECTCODE}ROMP##.###` (AWS data-center jobs)."""
    if not description or "ROMP" not in str(description).upper():
        return (None, None)
    d = str(description)
    m = re.search(r"ROMP\s*0*\d+(?:\.\d+)?", d, re.I)
    romp = m.group(0).upper().replace(" ", "") if m else None
    if romp:
        romp = re.sub(r"^ROMP0*", "ROMP", romp)
    d2 = _PREFIX_ROMP_RE.sub("", d)
    if template:
        d2 = re.sub(re.escape(template), "", d2, flags=re.I)
    eq = re.sub(r"\s+", " ", d2).strip(" -._") or None
    return (eq, romp)


def _norm_tokens(s) -> set:
    """Lowercase alphanumeric tokens, split on any separator. 'USB-XFMR-1.1A' ->
    {'usb','xfmr','1','1a'}."""
    import re as _re
    return {t for t in _re.split(r"[^a-z0-9]+", str(s or "").lower()) if t}


def equipment_matches(query: str, equip: str) -> bool:
    """True if every token of `query` appears in `equip` (token-subset, separator- and
    case-insensitive). So 'USB-1.1A' matches 'USB 1.1A', 'USB-XFMR-1.1A' and
    'SK.01A-USB 1.1A', but NOT 'USB-XFMR-1.1B' or 'USB-XFMR-11.1A'."""
    q = _norm_tokens(query)
    return bool(q) and q.issubset(_norm_tokens(equip))


def _type_name(lst: dict) -> str:
    t = lst.get("inspection_type")
    return (t or {}).get("name") if isinstance(t, dict) else (t or "")


def _loc_name(lst: dict) -> str:
    loc = lst.get("location")
    return (loc or {}).get("name") if isinstance(loc, dict) else (loc or "")


def list_rollup(client, project_id: int, company_id: int, *,
                types=DEFAULT_TYPES, romp: str | None = None,
                equipment: str | None = None) -> list[dict]:
    """Per-inspection completion rollup (one row per inspection) from the REST
    `checklist/lists` sweep — id, name, type, Equipment, ROMP, location, and the
    respondable/answered/conforming/deficient/N-A counts + % complete. Filter by
    type, ROMP, and/or equipment tag."""
    types_lc = {t.lower() for t in types} if types else None
    rows = []
    for l in client.inspection_lists(project_id, company_id):
        if types_lc and _type_name(l).lower() not in types_lc:
            continue
        cf = l.get("custom_fields") or {}
        eq = _cf_value(cf, CF_EQUIPMENT)
        rp = _cf_value(cf, CF_ROMP)
        if not eq or not rp:
            peq, prp = parse_equipment_romp(l.get("description"), l.get("list_template_name"))
            eq = eq or peq
            rp = rp or prp
        if romp and romp.upper() not in (rp or "").upper():
            continue
        if equipment and not equipment_matches(equipment, eq):
            continue
        respondable = l.get("respondable_item_count") or 0
        answered = l.get("inspected_item_count") or 0
        rows.append({"id": l.get("id"), "name": l.get("list_template_name") or l.get("name"),
                     "type": _type_name(l), "equipment": eq or "", "romp": rp or "",
                     "location": _loc_name(l) or "", "status": l.get("status"),
                     "respondable": respondable, "answered": answered,
                     "conforming": l.get("conforming_item_count") or 0,
                     "deficient": l.get("deficient_item_count") or 0,
                     "not_applicable": l.get("not_applicable_item_count") or 0,
                     "pct_complete": round(answered / respondable, 4) if respondable else None})
    return rows


def export_items_cube(client, project_id: int, company_id: int, out_path: str, *,
                      types=DEFAULT_TYPES, romp: str | None = None, respondable_only: bool = True,
                      page: int = 5000, max_rows: int | None = None, log=print) -> dict:
    """Fast path: pull item rows in bulk from the Enhanced Reports (360) Cube API
    (`v3_inspection_items` joined to `v3_inspections`), joining list-level
    Equipment/ROMP/Location from one REST `checklist/lists` sweep. One row per
    line item, streamed to CSV."""
    # 1) list-level attributes (Equipment cf12501 / ROMP cf12280 / Location / Name / Type)
    lists = client.inspection_lists(project_id, company_id)
    attrs = {}
    for l in lists:
        cf = l.get("custom_fields") or {}
        eq = _cf_value(cf, CF_EQUIPMENT)
        rp = _cf_value(cf, CF_ROMP)
        if not eq or not rp:
            peq, prp = parse_equipment_romp(l.get("description"), l.get("list_template_name"))
            eq = eq or peq
            rp = rp or prp
        attrs[str(l.get("id"))] = {"name": l.get("list_template_name") or l.get("name") or "",
                                   "type": _type_name(l), "equipment": eq or "", "romp": rp or "",
                                   "location": _loc_name(l) or ""}
    log(f"[export] {len(attrs)} inspection lists (attrs) loaded")

    filters = []
    if types:
        filters.append({"member": "v3_inspections.inspection_type", "operator": "equals", "values": list(types)})
    if respondable_only:
        filters.append({"member": "v3_inspection_items.respondable", "operator": "equals", "values": ["true"]})
    dims = ["v3_inspection_items.inspection_id", "v3_inspection_items.section",
            "v3_inspection_items.name", "v3_inspection_items.responded_with"]

    fh = open(out_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(fh)
    writer.writerow(COLUMNS)
    offset = 0
    counts = {"rows": 0, "answered": 0, "skipped_romp": 0}
    while True:
        j = client.reporting_load(project_id, company_id, dims, filters=filters, limit=page, offset=offset)
        data = j.get("data") or []
        if not data:
            break
        batch = []
        for row in data:
            iid = str(row.get("v3_inspection_items.inspection_id") or "")
            a = attrs.get(iid, {})
            if romp and romp.upper() not in (a.get("romp") or "").upper():
                counts["skipped_romp"] += 1
                continue
            resp = row.get("v3_inspection_items.responded_with") or "No Response"
            batch.append([iid, a.get("name", ""), a.get("type", ""), a.get("equipment", ""),
                          a.get("romp", ""), row.get("v3_inspection_items.section") or "",
                          row.get("v3_inspection_items.name") or "", resp, a.get("location", "")])
            if resp != "No Response":
                counts["answered"] += 1
        writer.writerows(batch)
        counts["rows"] += len(batch)
        offset += len(data)
        log(f"[export] {counts['rows']} rows (offset {offset})")
        if len(data) < page or (max_rows and counts["rows"] >= max_rows):
            break
    fh.close()
    counts.update(out=out_path, inspections=len(attrs), engine="cube")
    log(f"[export] DONE: {counts['rows']} rows ({counts['answered']} answered) -> {out_path}")
    return counts


def export_items(client, project_id: int, company_id: int, out_path: str, *,
                 types=DEFAULT_TYPES, romp: str | None = None, limit: int | None = None,
                 workers: int = 10, respondable_only: bool = True, log=print) -> dict:
    """Fetch inspection items and stream rows to CSV. Returns a summary dict."""
    types_lc = {t.lower() for t in types} if types else None
    lists = client.inspection_lists(project_id, company_id)
    log(f"[export] {len(lists)} inspection lists in project")

    def in_scope(lst):
        if types_lc and _type_name(lst).lower() not in types_lc:
            return False
        if romp:
            cf = lst.get("custom_fields") or {}
            rv = _cf_value(cf, CF_ROMP) or parse_equipment_romp(lst.get("description"), lst.get("list_template_name"))[1] or ""
            if romp.upper() not in rv.upper():
                return False
        return True

    scoped = [l for l in lists if in_scope(l)]
    if limit:
        scoped = scoped[:limit]
    log(f"[export] {len(scoped)} lists in scope (types={types}, romp={romp}, limit={limit})")

    lock = threading.Lock()
    counts = {"lists_done": 0, "rows": 0, "answered": 0, "errors": 0}
    fh = open(out_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(fh)
    writer.writerow(COLUMNS)

    def base_fields(lst):
        cf = lst.get("custom_fields") or {}
        eq = _cf_value(cf, CF_EQUIPMENT)
        rp = _cf_value(cf, CF_ROMP)
        if not eq or not rp:
            peq, prp = parse_equipment_romp(lst.get("description"), lst.get("list_template_name"))
            eq = eq or peq
            rp = rp or prp
        return {"id": lst.get("id"), "name": lst.get("list_template_name") or lst.get("name"),
                "type": _type_name(lst), "equipment": eq or "", "romp": rp or "",
                "location": _loc_name(lst) or ""}

    def work(lst):
        bf = base_fields(lst)
        try:
            secs = client.checklist_sections(project_id, company_id, lst["id"])
            secmap = {s["id"]: s.get("name", "") for s in secs}
            items = client.checklist_items(project_id, company_id, lst["id"])
        except Exception as exc:
            with lock:
                counts["errors"] += 1
            return
        rows = []
        for it in items:
            if respondable_only and not it.get("response_set"):
                continue
            resp = it.get("responded_with") or "No Response"
            rows.append([bf["id"], bf["name"], bf["type"], bf["equipment"], bf["romp"],
                         secmap.get(it.get("section_id"), ""), it.get("name", ""), resp, bf["location"]])
        with lock:
            writer.writerows(rows)
            counts["rows"] += len(rows)
            counts["answered"] += sum(1 for r in rows if r[7] != "No Response")
            counts["lists_done"] += 1
            if counts["lists_done"] % 250 == 0:
                log(f"[export] {counts['lists_done']}/{len(scoped)} lists, {counts['rows']} rows")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, scoped))
    fh.close()
    counts["out"] = out_path
    counts["lists_in_scope"] = len(scoped)
    log(f"[export] DONE: {counts['rows']} item rows ({counts['answered']} answered) "
        f"from {counts['lists_done']} lists -> {out_path}")
    return counts
