"""
Enhanced Reports (360) helpers for the Procore CLI.

The 360 report API is a Cube.js-style service:
  * data sources:  GET  /rest/v1.0/reporting_api/data-sets
  * schema:        GET  /rest/v1.0/reporting_api/data_sets/meta
  * data fetch:    POST /rest/v1.0/reporting_api/data_sets/{DataSet}/load
  * create report: POST /rest/v1.0/reports
  * delete report: DELETE /rest/v1.0/reports/{id}

CRITICAL create-report gotcha (learned the hard way): in the POST body,
`is_enrp`/`has_schedule` must be BOOLEANS and `x_pos/y_pos/width/height/sort_order`
must be INTEGERS. Sending them as strings makes the POST return only {id} and the
tabs silently do NOT persist.
"""

from __future__ import annotations

import base64
import os

# Cx/QC inspection-type ids (for the type-namespaced custom-field members).
TYPE_COMMISSIONING = "562949953463145"
TYPE_QUALITY = "562949953463143"
CF_EQUIP = "12501"   # Asset Tag Plate ID  -> Equipment
CF_ROMP = "12280"    # FBN Build ID(s)     -> ROMP

# Tab 1 — one row per inspection line item.
ITEM_COLUMNS = [
    "v3_inspection_items.inspection_id",
    "v3_inspections.template_name",
    "v3_inspections.inspection_type",
    "v3_inspection_items.section",
    "v3_inspection_items.name",
    "v3_inspection_items.responded_with",
    "v3_locations.name",
]

# Tab 2 — per-inspection rollup (Equipment/ROMP live here; they null out at item level).
# `v3_inspections.key` == `v3_inspection_items.inspection_id` — the join key back
# to the Items tab (without it, equipment can't be attached to item rows).
ROLLUP_COLUMNS = [
    "v3_inspections.key",
    "v3_inspections.template_name",
    "v3_inspections.inspection_type",
    f"v3_inspection_custom_fields.cf_{TYPE_COMMISSIONING}_{CF_EQUIP}",
    f"v3_inspection_custom_fields.cf_{TYPE_QUALITY}_{CF_EQUIP}",
    f"v3_inspection_custom_fields.cf_{TYPE_COMMISSIONING}_{CF_ROMP}",
    f"v3_inspection_custom_fields.cf_{TYPE_QUALITY}_{CF_ROMP}",
    "v3_locations.name",
    "v3_inspections.respondable_item_count",
    "v3_inspections.no_response_count",
    "v3_inspections.conforming_count",
    "v3_inspections.deficient_count",
    "v3_inspections.not_applicable_count",
]


def _ulid() -> str:
    return base64.b32encode(os.urandom(16)).decode().rstrip("=")[:26]


def _col(field: str, width: int = 200) -> dict:
    return {"field": field, "hidden": False, "pinned": None, "rowGroup": False,
            "rowGroupIndex": None, "sort": None, "sortIndex": None, "width": width}


def _type_filter(types) -> list:
    if not types:
        return []
    return [{"field": "v3_inspections.inspection_type", "type": "set", "include": True,
             "selected": [{"id": t, "label": t} for t in types]}]


def _tab(name: str, columns: list[str], order: int, types) -> dict:
    return {"name": name, "id": _ulid(),
            "data_set_config": {"dataSetKey": "Project_Management",
                                "columnState": [_col(c, 320 if c.endswith("items.name") else 200) for c in columns],
                                "filterState": _type_filter(types),
                                "x_pos": 0, "y_pos": 0, "width": 4, "height": 5},
            "sort_order": order, "label": "Project_Management", "dataSetKey": "Project_Management"}


def inspection_report_body(company_id, name: str, description: str = "",
                           types=("Commissioning", "Quality")) -> dict:
    """Build the create-report POST body for the 2-tab Inspection Items + Rollup report."""
    return {
        "report": {
            "id": "new", "name": name, "description": description,
            "users": [], "is_enrp": False, "has_schedule": False,
            "report_tabs_attributes": [
                _tab("Inspection Items", ITEM_COLUMNS, 0, types),
                _tab("Inspection Rollup", ROLLUP_COLUMNS, 1, types),
            ],
        },
        "data_set_table_custom_column_created_ids": [],
        "companyId": str(company_id),
        "locale": "en-US-x-owner",
    }
