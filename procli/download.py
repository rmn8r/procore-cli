"""
File downloads for the Procore CLI.

Procore serves attachments and documents through its File Access Service (FAS):
each attachment/file-version object carries a signed `url` like
`https://<shard>/fas/api/v5/files/.../<key>?...&sig=...`. That first request needs
the logged-in SESSION COOKIE (the sig alone 401s); it then 302-redirects to a
pre-signed S3 URL that returns the bytes. So every download here goes through the
authenticated session client with redirects followed.

Three entry points, all returning a JSON-able manifest:
  download_rfi(...)        -- every attachment on an RFI (question + responses)
  download_submittal(...)  -- every attachment on a submittal (+ approval log)
  download_documents(...)  -- files from the Documents tool, by folder-path filter

Downloads are GETs only, so the DELETE safety guard is never involved.
"""

from __future__ import annotations

import re
from pathlib import Path

# Guard rails so a single call can't accidentally pull a whole project.
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_MB = 1024          # skip any single file larger than this
_FAS_MARK = "/fas/"            # marks a Procore File-Access-Service download URL
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Image/evidence-photo extensions — skipped when a caller wants test reports only.
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".bmp",
              ".tif", ".tiff", ".svg"}


def _is_image(name: str, content_type: str | None = None) -> bool:
    if content_type and str(content_type).lower().startswith("image/"):
        return True
    return Path(name or "").suffix.lower() in _IMAGE_EXT


def _safe_name(name: str, fallback: str = "file") -> str:
    """Make a single path component safe on Windows/macOS/Linux."""
    name = (name or "").strip().strip(".")
    name = _ILLEGAL.sub("_", name)
    name = name.rstrip(". ")           # Windows forbids trailing dot/space
    return name[:200] or fallback


def _safe_relpath(name_with_path: str, fallback: str = "file") -> Path:
    """Sanitize each component of a Procore name_with_path into a safe relative
    path (also neutralizes any '..' traversal)."""
    parts = [p for p in re.split(r"[\\/]+", name_with_path or "") if p not in ("", ".", "..")]
    parts = [_safe_name(p, fallback) for p in parts]
    return Path(*parts) if parts else Path(fallback)


def _dedupe(path: Path) -> Path:
    """If path exists, insert ' (2)', ' (3)', ... before the suffix."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 2
    while True:
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def _stream_download(session, url: str, dest: Path, *, max_mb: int = DEFAULT_MAX_MB) -> dict:
    """Stream one FAS url to dest via the authenticated session. Returns a result
    dict (never raises for an HTTP error — reports it instead, so one bad file
    doesn't abort a batch)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with session._client.stream("GET", url, follow_redirects=True) as r:
            if r.status_code != 200:
                # Drain so the connection can be reused, then report.
                try:
                    r.read()
                except Exception:
                    pass
                return {"ok": False, "error": f"HTTP {r.status_code}", "path": str(dest)}
            cap = max_mb * 1024 * 1024
            written = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(chunk_size=256 * 1024):
                    written += len(chunk)
                    if written > cap:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return {"ok": False, "error": f"exceeds max_mb={max_mb}", "path": str(dest)}
                    fh.write(chunk)
        tmp.replace(dest)
        return {"ok": True, "path": str(dest), "bytes": written}
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": str(exc)[:200], "path": str(dest)}


def _harvest_attachments(obj, _seen: set | None = None) -> list[dict]:
    """Recursively find every attachment-shaped dict (a FAS `url` + a name) in a
    parsed RFI/submittal payload. Deduped by url. Works uniformly across the
    different shapes (question.attachments, responses[].attachments, approver
    responses, top-level attachments, …)."""
    _seen = _seen if _seen is not None else set()
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            url = o.get("url")
            nm = o.get("filename") or o.get("name")
            if isinstance(url, str) and _FAS_MARK in url and nm and url not in _seen:
                _seen.add(url)
                out.append({"url": url, "name": str(nm), "content_type": o.get("content_type")})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return out


def _manifest(out_dir: Path, results: list[dict], extra: dict | None = None) -> dict:
    ok = [r for r in results if r.get("ok")]
    errs = [r for r in results if not r.get("ok")]
    m = {
        "out_dir": str(out_dir.resolve()),
        "downloaded": len(ok),
        "failed": len(errs),
        "total_bytes": sum(r.get("bytes", 0) for r in ok),
        "files": [{"name": Path(r["path"]).name, "path": str(Path(r["path"]).resolve()),
                   "bytes": r.get("bytes")} for r in ok],
    }
    if errs:
        m["errors"] = [{"path": r.get("path"), "error": r.get("error")} for r in errs]
    if extra:
        m.update(extra)
    return m


# -- public entry points ------------------------------------------------------

def download_rfi(client, project_id: int, company_id: int, rfi_id: int,
                 out_dir: str | Path, *, label: str | None = None,
                 max_mb: int = DEFAULT_MAX_MB) -> dict:
    """Download every attachment on one RFI into out_dir/<label>/."""
    data = client.rfi(project_id, rfi_id, company_id)
    label = label or f"rfi-{_safe_name(str(data.get('full_number') or rfi_id))}"
    dest_dir = Path(out_dir) / _safe_name(label)
    atts = _harvest_attachments(data)
    results = []
    for a in atts:
        dest = _dedupe(dest_dir / _safe_name(a["name"]))
        results.append(_stream_download(client.s, a["url"], dest, max_mb=max_mb))
    return _manifest(dest_dir, results,
                     {"source": "rfi", "rfi": data.get("full_number") or rfi_id,
                      "subject": data.get("subject"), "attachments_found": len(atts)})


def download_submittal(client, project_id: int, company_id: int, submittal_id: int,
                       out_dir: str | Path, *, label: str | None = None,
                       max_mb: int = DEFAULT_MAX_MB) -> dict:
    """Download every attachment on one submittal (item + approval log) into
    out_dir/<label>/."""
    data = client.submittal(project_id, submittal_id, company_id)
    atts = _harvest_attachments(data)
    # The approval-workflow payload carries response attachments the item view omits.
    try:
        atts += _harvest_attachments(
            client.submittal_approvers(project_id, submittal_id, company_id),
            {a["url"] for a in atts})
    except Exception:
        pass
    label = label or f"submittal-{_safe_name(str(data.get('formatted_number') or submittal_id))}"
    dest_dir = Path(out_dir) / _safe_name(label)
    results = []
    for a in atts:
        dest = _dedupe(dest_dir / _safe_name(a["name"]))
        results.append(_stream_download(client.s, a["url"], dest, max_mb=max_mb))
    return _manifest(dest_dir, results,
                     {"source": "submittal",
                      "submittal": data.get("formatted_number") or submittal_id,
                      "title": data.get("title"), "attachments_found": len(atts)})


def _file_version_url(client, project_id: int, company_id: int, file_id: int) -> tuple[str | None, int | None]:
    """Resolve a document file id to its latest version's download url + size."""
    j = client.s.get_json(f"/rest/v1.0/files/{file_id}", company_id=company_id,
                          params={"project_id": project_id, "company_id": company_id})
    versions = j.get("file_versions") or []
    if not versions:
        return None, None
    # Highest version number is the current one; fall back to first.
    latest = max(versions, key=lambda v: v.get("number") or 0)
    pf = latest.get("prostore_file") or {}
    return (latest.get("url") or pf.get("url")), latest.get("size")


def download_documents(client, project_id: int, company_id: int, out_dir: str | Path, *,
                       folder: str | None = None, flat: bool = False,
                       limit: int = DEFAULT_MAX_FILES,
                       max_mb: int = DEFAULT_MAX_MB,
                       log=lambda m: None) -> dict:
    """Download files from the Documents tool. By default mirrors the Procore
    folder tree under out_dir (set flat=True to dump all into one folder).
    `folder` filters by a case-insensitive substring of each file's full path;
    without it, EVERY file in the project is in scope (bounded by `limit`)."""
    docs = client.documents_all(project_id, company_id)
    files = [x for x in docs if x.get("document_type") == "file" and not x.get("is_deleted")]
    if folder:
        f = folder.lower()
        files = [x for x in files
                 if f in (x.get("name_with_path") or x.get("name") or "").lower()]
    total_matched = len(files)
    truncated = total_matched > limit
    files = files[:limit]

    out_dir = Path(out_dir)
    results = []
    for i, x in enumerate(files, 1):
        fid = x.get("id")
        nwp = x.get("name_with_path") or x.get("name") or f"file-{fid}"
        log(f"[{i}/{len(files)}] {nwp}")
        try:
            url, _size = _file_version_url(client, project_id, company_id, fid)
        except Exception as exc:  # noqa: BLE001
            results.append({"ok": False, "error": f"version lookup failed: {str(exc)[:160]}",
                            "path": str(out_dir / _safe_relpath(nwp))})
            continue
        if not url:
            results.append({"ok": False, "error": "no file version/url",
                            "path": str(out_dir / _safe_relpath(nwp))})
            continue
        if flat:
            dest = _dedupe(out_dir / _safe_name(x.get("name") or f"file-{fid}"))
        else:
            dest = _dedupe(out_dir / _safe_relpath(nwp))
        results.append(_stream_download(client.s, url, dest, max_mb=max_mb))

    extra = {"source": "documents", "matched": total_matched, "attempted": len(files)}
    if folder:
        extra["folder_filter"] = folder
    if truncated:
        extra["truncated"] = True
        extra["note"] = (f"{total_matched} files matched; only the first {limit} were "
                         f"downloaded. Narrow with folder=... or raise limit.")
    return _manifest(out_dir, results, extra)


def _name_from_url(url: str) -> str:
    """Best-effort filename from a FAS/S3 url (used when no name is supplied)."""
    from urllib.parse import urlsplit, unquote
    path = unquote(urlsplit(url).path)
    base = path.rsplit("/", 1)[-1] if "/" in path else path
    return _safe_name(base, "download")


def download_url(client, url: str, out_dir: str | Path, *, name: str | None = None,
                 max_mb: int = DEFAULT_MAX_MB) -> dict:
    """Download ANY Procore file URL (a signed FAS 'download link' from any tool —
    checklist attachments/photos, an attachment you already looked up, etc.) through
    the authenticated session. This is the universal fix: these links need the
    logged-in session cookie and a followed redirect; a bare fetch just gets the
    redirect with no file. Give it the url (and optionally a filename)."""
    if not isinstance(url, str) or not url.strip():
        return {"out_dir": str(Path(out_dir).resolve()), "downloaded": 0, "failed": 1,
                "errors": [{"error": "empty url"}]}
    dest = _dedupe(Path(out_dir) / _safe_name(name or _name_from_url(url)))
    res = _stream_download(client.s, url, dest, max_mb=max_mb)
    return _manifest(Path(out_dir), [res], {"source": "url"})


def download_inspection(client, project_id: int, company_id: int, list_id: int,
                        out_dir: str | Path, *, label: str | None = None,
                        docs_only: bool = False, max_mb: int = DEFAULT_MAX_MB) -> dict:
    """Download ALL files on one inspection (checklist list), organized to mirror the
    checklist: each item-level file/evidence photo lands under
    <label>/<section>/<item# item-name>/, inspection-level attachments under
    <label>/_inspection/. Signed links are resolved automatically. The manifest's
    `by_line` maps every downloaded file back to the checklist line it belongs to, so a
    reviewer can see which attachment answers which item. Set docs_only=True to skip
    evidence photos and pull just the documents (test reports)."""
    detail = client.checklist_list(project_id, company_id, list_id)
    ident = detail.get("identifier") or detail.get("number") or list_id
    nm = detail.get("list_template_name") or detail.get("name") or "inspection"
    # list_id keeps two inspections that share an identifier (e.g. both "#2") apart.
    label = label or _safe_name(f"{nm} {ident} [{list_id}]")
    dest_dir = Path(out_dir) / _safe_name(label)

    # Line maps: item_id -> {number,name,section_id}; section_id -> section name.
    try:
        imap = {it.get("id"): it for it in client.checklist_items(project_id, company_id, list_id)}
    except Exception:
        imap = {}
    try:
        secs = {s.get("id"): s.get("name")
                for s in client.checklist_sections(project_id, company_id, list_id)}
    except Exception:
        secs = {}

    seen: set = set()
    jobs = []  # (att, line_info, subdir)

    # Inspection-level attachments (attached to the inspection, not a line).
    for a in _harvest_attachments(detail, seen):
        if docs_only and _is_image(a["name"], a.get("content_type")):
            continue
        jobs.append((a, {"scope": "inspection", "section": None, "item_number": None,
                         "item_name": None, "item_id": None}, Path("_inspection")))

    # Item-level files + evidence photos, each tagged with its checklist line.
    try:
        recs = client.checklist_item_attachments(project_id, company_id, list_id)
    except Exception:
        recs = []
    for rec in recs:
        att = rec.get("attachment") or {}
        url = att.get("url")
        name = att.get("filename") or att.get("name")
        if not (isinstance(url, str) and _FAS_MARK in url and name) or url in seen:
            continue
        if docs_only and _is_image(name, att.get("content_type")):
            seen.add(url)
            continue
        seen.add(url)
        iid = rec.get("item_id")
        it = imap.get(iid) or {}
        section = secs.get(it.get("section_id")) or "Unsectioned"
        num = it.get("number")
        iname = it.get("name") or ""
        line_folder = _safe_name(f"{num} {iname}".strip()) if (num or iname) else f"item-{iid}"
        subdir = _safe_relpath(f"{section}/{line_folder}")
        jobs.append(({"url": url, "name": name},
                     {"scope": "item", "section": section, "item_number": num,
                      "item_name": iname, "item_id": iid}, subdir))

    # Download, grouping results by checklist line.
    results = []
    lines: dict = {}
    for a, info, subdir in jobs:
        dest = _dedupe(dest_dir / subdir / _safe_name(a["name"]))
        res = _stream_download(client.s, a["url"], dest, max_mb=max_mb)
        results.append(res)
        key = info["item_id"] or "_inspection"
        ln = lines.setdefault(key, {k: info[k] for k in
                                    ("scope", "section", "item_number", "item_name", "item_id")}
                              | {"files": []})
        if res.get("ok"):
            ln["files"].append({"name": Path(res["path"]).name,
                                "path": str(Path(res["path"]).resolve()),
                                "bytes": res.get("bytes")})

    return _manifest(dest_dir, results,
                     {"source": "inspection", "inspection_id": list_id, "name": nm,
                      "identifier": ident,
                      "lines_with_attachments": sum(1 for k in lines if k != "_inspection"),
                      "by_line": list(lines.values())})


def download_equipment(client, project_id: int, company_id: int, equipment: str,
                       out_dir: str | Path, *, types=None, docs_only: bool = True,
                       max_mb: int = DEFAULT_MAX_MB, log=lambda m: None) -> dict:
    """Find every inspection/checklist for a piece of equipment (by its tag, e.g.
    'USB-1.1A') and download their attachments — organized per checklist and per
    checklist line — under out_dir/equipment-<tag>/. Defaults to docs_only=True so it
    pulls the TEST REPORTS (NETA, torque, test forms) and skips evidence photos; pass
    docs_only=False to include photos too. Optionally restrict to inspection types
    (e.g. ['Commissioning','Quality'] for Cx/QC). Returns a combined manifest."""
    from procli.inspections import (_cf_value, CF_EQUIPMENT, _type_name,
                                    parse_equipment_romp, equipment_matches)
    types_lc = {t.lower() for t in types} if types else None
    matched = []
    for l in client.inspection_lists(project_id, company_id):
        if types_lc and _type_name(l).lower() not in types_lc:
            continue
        cf = l.get("custom_fields") or {}
        eq = _cf_value(cf, CF_EQUIPMENT)
        if not eq:
            eq = parse_equipment_romp(l.get("description"), l.get("list_template_name"))[0]
        if eq and equipment_matches(equipment, eq):
            matched.append((l, eq))

    base = Path(out_dir) / _safe_name(f"equipment-{equipment}")
    checklists = []
    totals = {"downloaded": 0, "failed": 0, "total_bytes": 0}
    for l, eq in matched:
        log(f"[equipment] {l.get('id')} {l.get('list_template_name') or l.get('name')} ({_type_name(l)})")
        m = download_inspection(client, project_id, company_id, l["id"], base,
                                docs_only=docs_only, max_mb=max_mb)
        checklists.append({"inspection_id": l["id"], "name": m.get("name"),
                           "type": _type_name(l), "equipment": eq,
                           "downloaded": m["downloaded"], "failed": m["failed"],
                           "total_bytes": m["total_bytes"], "out_dir": m["out_dir"]})
        for k in totals:
            totals[k] += m.get(k, 0)

    return {"source": "equipment", "equipment": equipment,
            "out_dir": str(base.resolve()), "docs_only": docs_only,
            "matched_checklists": len(matched),
            "types": sorted({_type_name(l) for l, _ in matched}),
            **totals, "checklists": checklists}


def list_inspection_attachments(client, project_id: int, company_id: int, list_id: int) -> dict:
    """List (do NOT download) every attachment on an inspection, mapped to its checklist
    line, with each file's name, type, and signed download url. The manual-scour fallback:
    when the automated equipment→checklist→download path misses, a reviewer can see exactly
    what is attached where and pull specific files with download_url."""
    detail = client.checklist_list(project_id, company_id, list_id)
    try:
        imap = {it.get("id"): it for it in client.checklist_items(project_id, company_id, list_id)}
    except Exception:
        imap = {}
    try:
        secs = {s.get("id"): s.get("name")
                for s in client.checklist_sections(project_id, company_id, list_id)}
    except Exception:
        secs = {}

    seen: set = set()
    lines: dict = {}

    def add(info, name, content_type, url):
        key = info["item_id"] or "_inspection"
        ln = lines.setdefault(key, {**info, "files": []})
        ln["files"].append({"name": name, "content_type": content_type,
                            "is_image": _is_image(name, content_type), "url": url})

    for a in _harvest_attachments(detail, seen):
        add({"scope": "inspection", "section": None, "item_number": None,
             "item_name": None, "item_id": None}, a["name"], a.get("content_type"), a["url"])
    try:
        recs = client.checklist_item_attachments(project_id, company_id, list_id)
    except Exception:
        recs = []
    for rec in recs:
        att = rec.get("attachment") or {}
        url = att.get("url")
        name = att.get("filename") or att.get("name")
        if not (isinstance(url, str) and _FAS_MARK in url and name) or url in seen:
            continue
        seen.add(url)
        iid = rec.get("item_id")
        it = imap.get(iid) or {}
        add({"scope": "item", "section": secs.get(it.get("section_id")) or "Unsectioned",
             "item_number": it.get("number"), "item_name": it.get("name") or "",
             "item_id": iid}, name, att.get("content_type"), url)

    total = sum(len(l["files"]) for l in lines.values())
    return {"source": "inspection_attachments", "inspection_id": list_id,
            "name": detail.get("list_template_name") or detail.get("name"),
            "identifier": detail.get("identifier") or detail.get("number") or list_id,
            "total_attachments": total,
            "lines_with_attachments": sum(1 for k in lines if k != "_inspection"),
            "by_line": list(lines.values())}


def download_observation(client, project_id: int, company_id: int, observation_id: int,
                         out_dir: str | Path, *, label: str | None = None,
                         max_mb: int = DEFAULT_MAX_MB) -> dict:
    """Download every attachment/photo on one observation into out_dir/<label>/.
    The observation detail embeds its attachments[] with signed links directly."""
    data = client.observation(observation_id, project_id, company_id)
    atts = _harvest_attachments(data)
    num = data.get("number") or observation_id
    label = label or f"observation-{_safe_name(str(num))}"
    dest_dir = Path(out_dir) / _safe_name(label)
    results = []
    for a in atts:
        dest = _dedupe(dest_dir / _safe_name(a["name"]))
        results.append(_stream_download(client.s, a["url"], dest, max_mb=max_mb))
    return _manifest(dest_dir, results,
                     {"source": "observation", "observation_id": observation_id,
                      "number": num, "name": data.get("name"),
                      "attachments_found": len(atts)})
