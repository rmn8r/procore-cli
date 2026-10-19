"""Live probe: confirm the session works and discover the projects-list endpoint."""

from __future__ import annotations

from procli.session import ProcoreSession

CANDIDATES = [
    ("GET", "/rest/v1.0/me", {}),
    # projects-list candidates (company_id filled in after we learn it)
]

PROJECT_LIST_CANDIDATES = [
    "/rest/v1.1/projects?company_id={cid}",
    "/rest/v1.0/projects?company_id={cid}",
    "/rest/v1.0/companies/{cid}/projects",
    "/rest/v1.1/companies/{cid}/projects",
    "/rest/v1.0/projects?company_id={cid}&per_page=5",
]


def brief(obj, n=3):
    if isinstance(obj, list):
        return f"list[{len(obj)}] first_keys={list(obj[0].keys())[:12] if obj and isinstance(obj[0], dict) else obj[:n]}"
    if isinstance(obj, dict):
        return f"dict keys={list(obj.keys())[:15]}"
    return repr(obj)[:120]


with ProcoreSession() as s:
    print("csrf token loaded:", bool(s._csrf_token))

    r = s.get("/rest/v1.0/me")
    print(f"\nGET /rest/v1.0/me -> {r.status_code}")
    if r.status_code == 200:
        me = r.json()
        print("  ", brief(me))
        for k in ("id", "login", "name", "email_address", "company"):
            if k in me:
                v = me[k]
                print(f"   {k}: {v if k != 'email_address' else '<hidden>'}")

    # Companies
    for path in ("/rest/v1.0/companies", "/rest/v1.1/companies?per_page=100"):
        r = s.get(path)
        print(f"\nGET {path} -> {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print("  ", brief(data))
            comps = data if isinstance(data, list) else data.get("data", data)
            if isinstance(comps, list):
                for c in comps[:10]:
                    if isinstance(c, dict):
                        print(f"   company {c.get('id')}: {c.get('name')}")
            break

    cid = s.companies()[0]["id"]  # first accessible company
    print(f"\n--- probing projects-list with company_id={cid} ---")
    for tmpl in PROJECT_LIST_CANDIDATES:
        path = tmpl.format(cid=cid)
        try:
            r = s.get(path, company_id=cid)
            note = ""
            if r.status_code == 200:
                data = r.json()
                note = "  " + brief(data)
            print(f"GET {path} -> {r.status_code}{note}")
        except Exception as e:
            print(f"GET {path} -> ERROR {e}")
