"""
High-level Procore client built on the reverse-engineered internal REST API.

Every endpoint/param here was observed in a real browser session capture (see
captures/) and verified live. Methods return parsed JSON. Company/project can be
given by id or by (case-insensitive substring) name via the resolve_* helpers.
"""

from __future__ import annotations

from functools import lru_cache

from procli.session import ProcoreSession


class ProcoreClient:
    def __init__(self, session: ProcoreSession | None = None):
        self.s = session or ProcoreSession()

    def close(self):
        self.s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- identity / org ----------------------------------------------------
    def me(self) -> dict:
        return self.s.get_json("/rest/v1.0/me")

    @lru_cache(maxsize=1)
    def companies(self) -> list[dict]:
        return self.s.get_json("/rest/v1.0/companies")

    def resolve_company(self, ref: str | int) -> dict:
        comps = self.companies()
        # exact id
        try:
            rid = int(ref)
            for c in comps:
                if c["id"] == rid:
                    return c
        except (ValueError, TypeError):
            pass
        # name substring
        matches = [c for c in comps if str(ref).lower() in (c.get("name") or "").lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"No company matching {ref!r}. Available: "
                              + ", ".join(c.get('name', '?') for c in comps))
        raise LookupError(f"Ambiguous company {ref!r}: "
                          + ", ".join(c.get('name', '?') for c in matches))

    # -- projects ----------------------------------------------------------
    def projects(self, company_id: int, *, compact: bool = True) -> list[dict]:
        """Compact view = id/name/address/stage/status/open_items. Detailed view
        = full project records."""
        if compact:
            return self.s.get_json(f"/rest/v1.0/companies/{company_id}/projects",
                                   company_id=company_id)
        return self.s.get_json("/rest/v1.0/projects",
                               params={"company_id": company_id}, company_id=company_id)

    def project(self, company_id: int, project_id: int) -> dict:
        return self.s.get_json(f"/rest/v1.0/projects/{project_id}",
                               params={"company_id": company_id}, company_id=company_id)

    def resolve_project(self, company_id: int, ref: str | int) -> dict:
        projs = self.projects(company_id, compact=True)
        try:
            rid = int(ref)
            for p in projs:
                if p["id"] == rid:
                    return p
        except (ValueError, TypeError):
            pass
        matches = [p for p in projs if str(ref).lower() in (p.get("name") or "").lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"No project matching {ref!r} in company {company_id}.")
        raise LookupError(f"Ambiguous project {ref!r}: "
                          + ", ".join(p.get('name', '?') for p in matches[:10]))

    # -- RFIs --------------------------------------------------------------
    def rfis(self, project_id: int, company_id: int, *, page: int = 1,
             per_page: int = 150) -> list[dict]:
        return self.s.get_json(
            f"/rest/v1.0/projects/{project_id}/rfis",
            company_id=company_id,
            params={
                "view": "list", "serializer_view": "web_index",
                "page": page, "per_page": per_page,
                "sort[attribute]": "position", "sort[direction]": "desc",
                "filters[current_revision][]": "true", "as_datetimes": "true",
            },
        )

    def rfis_all(self, project_id: int, company_id: int, *, per_page: int = 300,
                 max_pages: int = 30) -> list[dict]:
        """All current-revision RFIs, following pagination."""
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            batch = self.rfis(project_id, company_id, page=page, per_page=per_page)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    def find_rfi_by_number(self, project_id: int, company_id: int,
                           number: str | int) -> dict | None:
        """Resolve a display number (212, "212", or "RFI-212") to the RFI list
        record (which carries the internal id). Returns None if not found."""
        target = str(number).strip().upper()
        if target.startswith("RFI-"):
            target = target[4:]
        for r in self.rfis_all(project_id, company_id):
            num = str(r.get("number") or "").strip().upper()
            full = str(r.get("full_number") or "").strip().upper()
            if num == target or full == target or full == f"RFI-{target}":
                return r
        return None

    def rfi(self, project_id: int, rfi_id: int, company_id: int) -> dict:
        return self.s.get_json(
            f"/rest/v1.1/projects/{project_id}/rfis/{rfi_id}",
            company_id=company_id,
            params={"with_drawing_info": "true", "as_datetimes": "true"},
        )

    # -- Submittals --------------------------------------------------------
    def submittals(self, project_id: int, company_id: int, *, page: int = 1,
                   per_page: int = 150) -> list[dict]:
        return self.s.get_json(
            f"/rest/v1.0/projects/{project_id}/submittals",
            company_id=company_id,
            params={
                "view": "list", "serializer_view": "minimal_list",
                "sort": "-number", "page": page, "per_page": per_page,
            },
        )

    def submittals_all(self, project_id: int, company_id: int, *, per_page: int = 300,
                       max_pages: int = 30) -> list[dict]:
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            batch = self.submittals(project_id, company_id, page=page, per_page=per_page)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    def find_submittal_by_number(self, project_id: int, company_id: int,
                                 number: str | int) -> dict | None:
        """Resolve a display number to the submittal list record. Submittals can
        carry a compound number like "26 05 00-001"; match defensively against
        the several number-ish fields Procore uses."""
        target = str(number).strip().upper()
        for s in self.submittals_all(project_id, company_id):
            for field in ("formatted_number", "full_number", "number"):
                v = str(s.get(field) or "").strip().upper()
                if v and v == target:
                    return s
        return None

    def submittal(self, project_id: int, submittal_id: int, company_id: int) -> dict:
        return self.s.get_json(
            f"/rest/v1.1/projects/{project_id}/submittals/{submittal_id}",
            company_id=company_id,
            params={
                "with_drawing_info": "true", "as_datetimes": "true",
                "display_company_with_name": "true", "serializer_view": "minimal_show",
            },
        )

    def submittal_approvers(self, project_id: int, submittal_id: int,
                            company_id: int) -> dict:
        """Approver list + response log for a submittal (the approval workflow)."""
        return self.s.get_json(
            f"/rest/v1.1/projects/{project_id}/submittals/{submittal_id}"
            f"/submittal_approvers_and_attachments_data",
            company_id=company_id,
            params={"locale": "en-US-x-owner", "omit_response_modal_props": "true"},
        )

    # -- Observations ------------------------------------------------------
    def observations(self, project_id: int, company_id: int) -> list | dict:
        return self.s.get_json("/rest/v1.0/observations/items",
                               company_id=company_id,
                               params={"project_id": project_id})

    def observations_all(self, project_id: int, company_id: int, *, per_page: int = 300,
                         max_pages: int = 40) -> list[dict]:
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            batch = self.s.get_json("/rest/v1.0/observations/items", company_id=company_id,
                                    params={"project_id": project_id, "page": page, "per_page": per_page})
            batch = batch if isinstance(batch, list) else (batch.get("data") or [])
            if not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    def observation(self, item_id: int, project_id: int, company_id: int) -> dict:
        return self.s.get_json(f"/rest/v1.0/observations/items/{item_id}",
                               company_id=company_id,
                               params={"company_id": company_id, "project_id": project_id})

    def observation_response_logs(self, item_id: int, project_id: int, company_id: int) -> list[dict]:
        return self.s.get_json(f"/rest/v1.0/observations/items/{item_id}/response_logs",
                               company_id=company_id,
                               params={"project_id": project_id, "page": 1, "per_page": 100})

    def find_observation_by_number(self, project_id: int, company_id: int,
                                   number: str | int) -> dict | None:
        target = str(number).strip()
        for o in self.observations_all(project_id, company_id):
            if str(o.get("number") or "").strip() == target:
                return o
        return None

    # -- Directory ---------------------------------------------------------
    def vendors(self, project_id: int, company_id: int) -> list[dict]:
        return self.s.get_json(f"/rest/v1.0/projects/{project_id}/vendors",
                               company_id=company_id,
                               params={"view": "compact", "sort": "name"})

    def project_members(self, project_id: int, company_id: int) -> list[dict]:
        return self.s.get_json(f"/rest/v1.0/projects/{project_id}/project_memberships",
                               company_id=company_id)

    # -- Inspections (checklist lists) -------------------------------------
    def inspection_lists(self, project_id: int, company_id: int, *, per_page: int = 1000,
                         max_pages: int = 40) -> list[dict]:
        """All inspection lists with full attributes INCLUDING custom_fields
        (the non-grouped `checklist/lists` endpoint carries them; grouped_index
        does not)."""
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            batch = self.s.get_json(f"/rest/v1.0/projects/{project_id}/checklist/lists",
                                    company_id=company_id, params={"page": page, "per_page": per_page})
            if not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    # -- Documents ---------------------------------------------------------
    def documents_all(self, project_id: int, company_id: int) -> list[dict]:
        """All documents (files + folders, flat) with name/name_with_path/parent_id/
        document_type. NOTE: this endpoint returns the full tree in one response;
        passing page/per_page silently switches it to a folders-only partial view,
        so we deliberately send no pagination params."""
        data = self.s.get_json(f"/rest/v1.0/projects/{project_id}/documents",
                               company_id=company_id)
        return data if isinstance(data, list) else (data.get("data") or [])

    # -- Enhanced Reports (360) -------------------------------------------
    def reports_list(self, project_id: int, company_id: int) -> dict:
        """The project's saved reports (myReports / mySharedReports / templates)."""
        return self.s.get_json(f"/{project_id}/project/reports/load_reports",
                               company_id=company_id,
                               params={"company_id": company_id, "project_id": project_id})

    def report_get(self, project_id: int, company_id: int, report_id: int | str) -> dict:
        return self.s.get_json(f"/rest/v1.0/reports/{report_id}", company_id=company_id,
                               params={"company_id": company_id, "project_id": project_id})

    def report_create(self, project_id: int, company_id: int, body: dict) -> dict:
        resp = self.s.request("POST", "/rest/v1.0/reports", company_id=company_id,
                              params={"company_id": company_id, "project_id": project_id},
                              json_body=body)
        resp.raise_for_status()
        return resp.json()

    def report_delete(self, project_id: int, company_id: int, report_id: int | str) -> int:
        resp = self.s.request("DELETE", f"/rest/v1.0/reports/{report_id}", company_id=company_id,
                              params={"company_id": company_id, "project_id": project_id})
        return resp.status_code

    def reporting_load(self, project_id: int, company_id: int, dimensions: list[str], *,
                       measures: list[str] | None = None, filters: list[dict] | None = None,
                       limit: int = 5000, offset: int = 0,
                       data_set: str = "Project_Management", total: bool = False) -> dict:
        """Query the Enhanced Reports (360) Cube API. Returns the parsed payload
        (with a `data` list of row dicts keyed by `cube.member`)."""
        body = {"query": {"dimensions": dimensions, "measures": measures or [],
                          "filters": filters or [], "limit": limit, "offset": offset,
                          "custom_columns": {"dimensions": {}, "measures": {}}, "total": total},
                "locale": "en-US-x-owner"}
        resp = self.s.request("POST", f"/rest/v1.0/reporting_api/data_sets/{data_set}/load",
                              company_id=company_id,
                              params={"company_id": company_id, "project_id": project_id},
                              json_body=body)
        resp.raise_for_status()
        return resp.json()

    def checklist_list(self, project_id: int, company_id: int, list_id: int) -> dict:
        """One inspection (checklist list) with its detail, INCLUDING the list-level
        `attachments[]` (each carries a signed FAS download `url`). The item-level
        `attachments`/`list_items` sweep does NOT carry these."""
        return self.s.get_json(f"/rest/v1.0/projects/{project_id}/checklist/lists/{list_id}",
                               company_id=company_id,
                               params={"project_id": project_id, "company_id": company_id})

    def checklist_item_attachments(self, project_id: int, company_id: int, list_id: int) -> list[dict]:
        """All ITEM-level attachments + photos for an inspection, each wrapping an
        `attachment` with a signed FAS `url`. This is the endpoint the web app hits
        when an item is opened (filterable by item_id; filtering by list_id returns
        the whole list in one call). The list detail only carries list-level
        attachments, so this is needed for evidence photos / per-item files."""
        data = self.s.get_json(
            f"/rest/v1.0/projects/{project_id}/checklist/list_item_attachments",
            company_id=company_id,
            params={"filters[list_id]": list_id, "view": "flat_v2"},
        )
        return data if isinstance(data, list) else (data.get("data") or [])

    def checklist_sections(self, project_id: int, company_id: int, list_id: int) -> list[dict]:
        return self.s.get_json(f"/rest/v1.1/projects/{project_id}/checklist/list_sections",
                               company_id=company_id, params={"filters[list_id]": list_id})

    def checklist_items(self, project_id: int, company_id: int, list_id: int, *,
                        per_page: int = 1000, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            batch = self.s.get_json(f"/rest/v1.1/projects/{project_id}/checklist/list_items",
                                    company_id=company_id,
                                    params={"filters[list_id]": list_id, "page": page, "per_page": per_page})
            if not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    # -- Correspondence (implemented as "generic tools") -------------------
    def correspondence_types(self, project_id: int) -> list[dict]:
        """The project's correspondence tools as {name, generic_tool_id}. Sourced
        from the tool's own type-filter dropdown feed."""
        data = self.s.get_json(
            f"/{project_id}/project/correspondence_types/generic_tool_id_filter_options"
        )
        return [{"name": o.get("value"), "generic_tool_id": int(o["key"])}
                for o in data if o.get("key")]

    def resolve_correspondence_type(self, project_id: int, ref: str | int) -> dict:
        types = self.correspondence_types(project_id)
        try:
            rid = int(ref)
            for t in types:
                if t["generic_tool_id"] == rid:
                    return t
        except (ValueError, TypeError):
            pass
        matches = [t for t in types if str(ref).lower() in (t["name"] or "").lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"No correspondence type matching {ref!r}. Available: "
                              + ", ".join(t["name"] for t in types))
        raise LookupError(f"Ambiguous correspondence type {ref!r}: "
                          + ", ".join(t["name"] for t in matches))

    def correspondence_items(self, project_id: int, company_id: int, generic_tool_id: int,
                             *, page: int = 1, per_page: int = 100) -> list[dict]:
        return self.s.get_json(
            f"/rest/v0.0/projects/{project_id}/correspondence_type_items",
            company_id=company_id,
            params={
                "page": page, "per_page": per_page,
                "view": "with_permissions_reduced",
                "filters[generic_tool_id][]": generic_tool_id,
                "sort": "-updated_at",
            },
        )

    def correspondence_items_all(self, project_id: int, company_id: int, generic_tool_id: int,
                                 *, per_page: int = 100, max_pages: int = 30) -> list[dict]:
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            batch = self.correspondence_items(project_id, company_id, generic_tool_id,
                                              page=page, per_page=per_page)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    def find_correspondence_by_number(self, project_id: int, company_id: int,
                                      generic_tool_id: int, number: str | int) -> dict | None:
        """Match a correspondence item by its display position (e.g. "PB-42")."""
        target = str(number).strip().upper()
        for it in self.correspondence_items_all(project_id, company_id, generic_tool_id):
            if str(it.get("position") or "").strip().upper() == target:
                return it
        return None

    def correspondence_item(self, project_id: int, company_id: int, generic_tool_id: int,
                            item_id: int) -> dict:
        return self.s.get_json(
            f"/rest/v0.0/projects/{project_id}/generic_tools/{generic_tool_id}"
            f"/generic_tool_items/{item_id}",
            company_id=company_id,
            params={"view": "web", "locale": "en-US-x-owner"},
        )

    def correspondence_responses(self, project_id: int, company_id: int, generic_tool_id: int,
                                 item_id: int) -> list[dict]:
        return self.s.get_json(
            f"/rest/v1.0/projects/{project_id}/generic_tools/{generic_tool_id}"
            f"/generic_tool_items/{item_id}/generic_tool_item_responses",
            company_id=company_id,
        )
