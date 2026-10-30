"""Papers as checkable sources: stable identifiers, metadata status, full-text access and claim checks.

A search hit, an existing paper, a read full text and a checked claim are different facts. A failed search is
logged as a failure, never as "no related work".
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import documents
from .schema import PAPER_ASSESSMENTS
from .store import Store
from .util import RLError, now_utc

ARXIV_RE = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)?(?P<id>\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})"
                      r"(?:v(?P<v>\d+))?(?:\.pdf)?$", re.I)
DOI_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)?(?P<doi>10\.\d{4,9}/\S+)$", re.I)
ATOM = {"a": "http://www.w3.org/2005/Atom"}
USER_AGENT = "smooth-research/0.1 (research record keeping)"


def normalize(identifier: str) -> dict:
    text = identifier.strip()
    m = ARXIV_RE.match(text)
    if m:
        return {"arxiv_id": m["id"].lower(), "arxiv_version": f"v{m['v']}" if m["v"] else None}
    m = DOI_RE.match(text)
    if m:
        return {"doi": m["doi"].lower().rstrip(".")}
    raise RLError("INVALID", f"{identifier!r} is neither an arXiv id (2106.09685[v2]) nor a DOI (10.xxxx/...)")


def _log(store: Store, entry: dict) -> None:
    path = store.project.dir("literature") / "queries.jsonl"
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps({"at": now_utc(), **entry}, ensure_ascii=False) + "\n")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt == 0:
                time.sleep(3)
                continue
            raise RLError("UNAVAILABLE", f"{url}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RLError("UNAVAILABLE", f"{url}: {exc}") from None
    raise RLError("UNAVAILABLE", f"{url}: rate limited")


def _arxiv_entries(xml: bytes) -> list[dict]:
    out = []
    for entry in ET.fromstring(xml).findall("a:entry", ATOM):
        link = entry.findtext("a:id", default="", namespaces=ATOM)
        m = re.search(r"abs/(.+?)(v\d+)?$", link)
        if not m:
            continue
        out.append({"arxiv_id": m.group(1).lower(), "arxiv_version": m.group(2),
                    "title": " ".join((entry.findtext("a:title", default="", namespaces=ATOM)).split()),
                    "authors": [a.findtext("a:name", default="", namespaces=ATOM)
                                for a in entry.findall("a:author", ATOM)],
                    "year": int((entry.findtext("a:published", default="0000", namespaces=ATOM))[:4] or 0) or None,
                    "url": f"https://arxiv.org/abs/{m.group(1)}"})
    return out


def fetch_metadata(ids: dict) -> dict:
    if ids.get("arxiv_id"):
        query = ids["arxiv_id"] + (ids.get("arxiv_version") or "")
        entries = _arxiv_entries(_get("https://export.arxiv.org/api/query?" +
                                      urllib.parse.urlencode({"id_list": query})))
        if not entries or not entries[0]["title"]:
            raise RLError("NOT_FOUND", f"arXiv has no entry for {query}")
        return {**entries[0], "metadata_source": "export.arxiv.org"}
    data = json.loads(_get(f"https://api.crossref.org/works/{urllib.parse.quote(ids['doi'])}"))["message"]
    year = ((data.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return {"title": " ".join((data.get("title") or [""])[0].split()), "year": year,
            "authors": [" ".join(filter(None, (a.get("given"), a.get("family")))) for a in data.get("author") or []],
            "url": data.get("URL"), "metadata_source": "api.crossref.org"}


def add_paper(store: Store, identifier: str | None = None, title: str | None = None, authors: list[str] = (),
              year: int | None = None, url: str | None = None, fetch: bool = False) -> dict:
    ids = normalize(identifier) if identifier else {}
    meta, status, fetch_error = {}, "unverified", None
    if fetch and ids:
        try:
            meta, status = fetch_metadata(ids), "verified"
            _log(store, {"op": "fetch", "ids": ids, "source": meta["metadata_source"], "ok": True})
        except RLError as exc:
            fetch_error, status = exc.message, "failed"
            _log(store, {"op": "fetch", "ids": ids, "ok": False, "error": exc.message})
    title = meta.get("title") or title
    if not title:
        raise RLError("INVALID", "a paper needs --title (or an identifier with --fetch)")
    with store.write() as tx:
        papers = tx.all("Paper")
        for p in papers:
            same = (ids.get("arxiv_id") and p.get("arxiv_id") == ids["arxiv_id"]) or (ids.get("doi") and p.get("doi") == ids["doi"])
            if same:
                changes = {}
                if ids.get("arxiv_version") and ids["arxiv_version"] != p.get("arxiv_version"):
                    changes["arxiv_version"] = ids["arxiv_version"]
                if status == "verified" and p["metadata_status"] != "verified":
                    changes.update(metadata_status="verified", metadata_source=meta.get("metadata_source"),
                                   fetched_at=now_utc(), title=title, authors=meta.get("authors"), year=meta.get("year"))
                if changes:
                    tx.update(p["id"], changes)
                return {"id": p["id"], "duplicate": True, "summary": f"{p['id']} already registered: {p['title']}"}
        similar = [p["id"] for p in papers if _similar(p["title"], title)]
        pid = tx.next_id("Paper")
        tx.create("Paper", {"id": pid, "title": title, "authors": list(meta.get("authors") or authors) or None,
                            "year": meta.get("year") or year, "doi": ids.get("doi"), "arxiv_id": ids.get("arxiv_id"),
                            "arxiv_version": meta.get("arxiv_version") or ids.get("arxiv_version"),
                            "url": meta.get("url") or url, "metadata_status": status, "fulltext_access": "unknown",
                            "metadata_source": meta.get("metadata_source"), "fetched_at": now_utc() if meta else None})
        tx.event("paper.added", pid, title)
    notes = []
    if similar:
        notes.append(f"similar titles {', '.join(similar)} — check whether they are versions of one work")
    if fetch_error:
        notes.append(f"metadata fetch failed ({fetch_error}); recorded as failed, not as absent")
    return {"id": pid, "summary": f"{pid} [{status}] {title}" + ("; " + "; ".join(notes) if notes else "")}


def _similar(a: str, b: str) -> bool:
    words = lambda s: set(re.findall(r"[a-z0-9]+", s.lower())) - {"a", "an", "the", "of", "for", "and", "in", "on", "with"}
    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.8


def search(store: Store, query: str, limit: int = 5) -> dict:
    params = {"search_query": f"all:{query}", "start": 0, "max_results": limit, "sortBy": "relevance"}
    try:
        entries = _arxiv_entries(_get("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)))
    except RLError as exc:
        _log(store, {"op": "search", "source": "arxiv", "params": params, "ok": False, "error": exc.message})
        raise RLError("UNAVAILABLE", f"search failed ({exc.message}); this says nothing about whether related "
                                     "work exists") from None
    _log(store, {"op": "search", "source": "arxiv", "params": params, "ok": True,
                 "results": [e["arxiv_id"] + (e["arxiv_version"] or "") for e in entries]})
    lines = [f"arXiv:{e['arxiv_id']}{e['arxiv_version'] or ''} ({e['year']}) {e['title']}" for e in entries]
    return {"results": entries, "summary": "\n".join([f"{len(entries)} search hit(s) for {query!r} "
                                                      "(hits are not checked claims; add with rl paper add <id> --fetch)",
                                                      *lines])}


def set_access(store: Store, pid: str, fulltext_access: str, digest: str | None = None) -> dict:
    with store.write() as tx:
        tx.require(pid, "Paper")
        tx.update(pid, {"fulltext_access": fulltext_access, "content_digest": digest})
        tx.event("paper.access", pid, fulltext_access)
    return {"id": pid, "summary": f"{pid} full text {fulltext_access}"}


def link_paper(store: Store, source: str, paper: str, relation: str, assessment: str, locator: str | None = None,
               note: str | None = None, note_file: str | None = None) -> dict:
    """Relate a paper to a hypothesis claim (nearest_work, motivated_by, or cites with a role in the note)."""
    if relation not in ("nearest_work", "motivated_by", "cites"):
        raise RLError("INVALID", "relation is nearest_work, motivated_by or cites")
    if assessment not in PAPER_ASSESSMENTS:
        raise RLError("INVALID", f"assessment is one of {', '.join(PAPER_ASSESSMENTS)}")
    if assessment == "checked" and not locator:
        raise RLError("INVALID", "a checked claim names its exact locator (section, equation, table, page)")
    prep = documents.prepare(store.project, note_file, "paper_note") if note_file else None
    with store.write() as tx:
        src, p = tx.require(source), tx.require(paper, "Paper")
        if assessment in ("fulltext_read", "checked") and p["fulltext_access"] != "available":
            tx.update(paper, {"fulltext_access": "available"})
        ref = documents.register(tx, prep) if prep else None
        if ref:
            tx.update(paper, {"note_refs": (p.get("note_refs") or []) + [ref]})
        tx.link(relation, source, paper, source_revision=src.get("revision"), assessment=assessment,
                locator=locator, note=note or ref)
        tx.event("paper.link", source, f"{source} {relation} {paper} ({assessment}{', ' + locator if locator else ''})")
    return {"summary": f"{source}@{src.get('revision')} {relation} {paper} [{assessment}]"
                       + (f" at {locator}" if locator else "")}
