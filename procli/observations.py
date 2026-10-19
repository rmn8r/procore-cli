"""
Observations helpers for the Procore CLI — brings Observations to parity with
Inspections (list/filter, detail, export). One row per observation.

Observation ROMP = custom field 12280 (FBN Build ID(s)); Equipment = cf 12501
(Asset Tag Plate ID) — same custom fields as inspections. Status values:
initiated (open) / ready_for_review / not_accepted / closed.
"""

from __future__ import annotations

import csv

CF_EQUIPMENT = "custom_field_12501"
CF_ROMP = "custom_field_12280"

COLUMNS = ["Observation ID", "Number", "Name", "Type", "Status", "Priority", "Trade",
           "Equipment", "ROMP", "Location", "Assignees", "Created By", "Created At",
           "Due Date", "Closed At"]


def _cf_value(cf: dict, key: str):
    ent = (cf or {}).get(key) or {}
    val = ent.get("value")
    if val is None:
        return None
    if isinstance(val, list):
        labels = [x.get("label") for x in val if isinstance(x, dict) and x.get("label")]
        return ", ".join(labels) if labels else None
    return str(val).strip() or None


def _name(x):
    return (x or {}).get("name") if isinstance(x, dict) else x


def _assignees(o) -> str:
    a = o.get("assignees") or ([o["assignee"]] if o.get("assignee") else [])
    return ", ".join(x.get("name", "") for x in a if isinstance(x, dict))


def row_of(o: dict) -> dict:
    cf = o.get("custom_fields") or {}
    return {
        "id": o.get("id"), "number": o.get("number"), "name": o.get("name"),
        "type": _name(o.get("type")), "status": o.get("status"), "priority": o.get("priority"),
        "trade": _name(o.get("trade")), "equipment": _cf_value(cf, CF_EQUIPMENT) or "",
        "romp": _cf_value(cf, CF_ROMP) or "", "location": _name(o.get("location")) or "",
        "assignees": _assignees(o), "created_by": _name(o.get("created_by")),
        "created_at": (o.get("created_at") or "")[:10], "due_date": o.get("due_date") or "",
        "closed_at": (o.get("closed_at") or "")[:10],
    }


def _match(o: dict, status, romp, type_) -> bool:
    if status and str(o.get("status") or "").lower() not in {s.strip().lower() for s in status}:
        return False
    if type_ and (type_.lower() not in str(_name(o.get("type")) or "").lower()):
        return False
    if romp:
        rv = _cf_value(o.get("custom_fields") or {}, CF_ROMP) or ""
        if romp.upper() not in rv.upper():
            return False
    return True


def list_rollup(client, project_id: int, company_id: int, *,
                status=None, romp=None, type_=None) -> list[dict]:
    obs = client.observations_all(project_id, company_id)
    return [row_of(o) for o in obs if _match(o, status, romp, type_)]


def export(client, project_id: int, company_id: int, out_path: str, *,
           status=None, romp=None, type_=None, log=print) -> dict:
    rows = list_rollup(client, project_id, company_id, status=status, romp=romp, type_=type_)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r["id"], r["number"], r["name"], r["type"], r["status"], r["priority"],
                        r["trade"], r["equipment"], r["romp"], r["location"], r["assignees"],
                        r["created_by"], r["created_at"], r["due_date"], r["closed_at"]])
    log(f"[obs export] {len(rows)} observations -> {out_path}")
    return {"rows": len(rows), "out": out_path}
