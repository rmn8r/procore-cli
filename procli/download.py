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
                out.append({"url": url, "name": str(nm)})
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
