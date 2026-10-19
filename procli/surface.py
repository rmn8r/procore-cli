"""
Surface map — a lightweight index of what exists in a Procore project so an
agent can orient itself, then drill in with the typed tools.

  overview(...)  -> per-domain counts + category breakdowns (the map)
  roster(...)    -> id/number/name/status list for ONE domain (the names)

Domains: rfis, submittals, observations, inspections, correspondence, documents.
Overview fans the domains out across threads (httpx client is thread-safe) and is
robust: a failure in one domain is reported inline, not fatal.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DOMAINS = ["rfis", "submittals", "observations", "inspections", "correspondence", "documents"]


def _tn(x, key="type"):
    t = x.get(key)
    return (t or {}).get("name") if isinstance(t, dict) else t


def _status_name(x):
    s = x.get("status")
    return (s or {}).get("name") if isinstance(s, dict) else s


def _romp_of(x):
    from procli.inspections import _cf_value, parse_equipment_romp, CF_ROMP
    r = _cf_value(x.get("custom_fields") or {}, CF_ROMP)
    if not r:
        _, r = parse_equipment_romp(x.get("description"), x.get("list_template_name"))
    return r or ""


def overview(client, project_id: int, company_id: int) -> dict:
    pid, cid = project_id, company_id

    def rfis():
        l = client.rfis_all(pid, cid)
        return {"count": len(l), "by_status": dict(Counter(str(x.get("status")) for x in l))}

    def submittals():
        l = client.submittals_all(pid, cid)
        return {"count": len(l), "by_status": dict(Counter(str(_status_name(x)) for x in l))}

    def observations():
        l = client.observations_all(pid, cid)
        return {"count": len(l),
                "by_status": dict(Counter(str(x.get("status")) for x in l)),
                "by_type": dict(Counter(_tn(x) for x in l).most_common(12))}

    def inspections():
        l = client.inspection_lists(pid, cid)
        return {"count": len(l),
                "by_type": dict(Counter(_tn(x, "inspection_type") for x in l)),
                "by_romp": dict(sorted(Counter(_romp_of(x) or "(none)" for x in l).items())),
                "templates": len({x.get("list_template_name") for x in l})}

    def correspondence():
        types = client.correspondence_types(pid)
        return {"type_count": len(types),
                "types": [{"name": t["name"], "tool_id": t["generic_tool_id"]} for t in types]}

    def documents():
        l = client.documents_all(pid, cid)
        folders = [x for x in l if x.get("document_type") == "folder"]
        files = sum(1 for x in l if x.get("document_type") == "file")
        top = [x.get("name") for x in folders if not x.get("parent_id")]
        return {"count": len(l), "files": files, "folders": len(folders), "top_folders": top[:40]}

    jobs = {"rfis": rfis, "submittals": submittals, "observations": observations,
            "inspections": inspections, "correspondence": correspondence, "documents": documents}
    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception as exc:  # noqa: BLE001
                out[k] = {"error": str(exc)[:160]}
    return out


def roster(client, project_id: int, company_id: int, domain: str, *,
           status=None, romp=None, type_=None, folder=None, limit: int = 1000) -> dict:
    pid, cid = project_id, company_id
    domain = domain.lower().rstrip("s") + "s" if domain.lower() in ("rfi",) else domain.lower()

    if domain == "rfis":
        rows = [{"id": x.get("id"), "number": x.get("full_number"),
                 "subject": x.get("subject"), "status": x.get("status")}
                for x in client.rfis_all(pid, cid)]
    elif domain == "submittals":
        rows = [{"id": x.get("id"), "number": x.get("formatted_number"),
                 "title": x.get("title"), "status": _status_name(x)}
                for x in client.submittals_all(pid, cid)]
    elif domain == "observations":
        from procli.observations import _match
        stat = [status] if status else None
        rows = [{"id": o.get("id"), "number": o.get("number"), "name": o.get("name"),
                 "type": _tn(o), "status": o.get("status")}
                for o in client.observations_all(pid, cid) if _match(o, stat, romp, type_)]
    elif domain == "inspections":
        rows = []
        for x in client.inspection_lists(pid, cid):
            rp = _romp_of(x)
            tp = _tn(x, "inspection_type")
            if romp and romp.upper() not in (rp or "").upper():
                continue
            if type_ and type_.lower() not in (tp or "").lower():
                continue
            if status and status.lower() != str(x.get("status") or "").lower():
                continue
            rows.append({"id": x.get("id"), "name": x.get("list_template_name"),
                         "romp": rp, "type": tp, "status": x.get("status")})
    elif domain in ("correspondence", "correspondences"):
        if type_:
            t = client.resolve_correspondence_type(pid, type_)
            rows = [{"id": i.get("id"), "position": i.get("position"),
                     "title": i.get("title"), "status": i.get("status")}
                    for i in client.correspondence_items_all(pid, cid, t["generic_tool_id"])]
        else:
            rows = [{"name": t["name"], "tool_id": t["generic_tool_id"]}
                    for t in client.correspondence_types(pid)]
    elif domain == "documents":
        rows = []
        for x in client.documents_all(pid, cid):
            path = x.get("name_with_path") or x.get("name")
            if folder and folder.lower() not in (path or "").lower():
                continue
            rows.append({"id": x.get("id"), "type": x.get("document_type"),
                         "name": x.get("name"), "path": path})
    else:
        raise ValueError(f"unknown domain: {domain!r} (use one of {DOMAINS})")

    total = len(rows)
    return {"domain": domain, "total": total, "returned": min(total, limit), "items": rows[:limit]}
