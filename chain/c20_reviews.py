"""Disease-review harvest for one named MeSH id."""

from __future__ import annotations

from datetime import date
import re
from urllib.error import URLError

from libraries import (
    Paper,
    disease_review_query,
    merge_by_id,
    take_qualified,
)
from llm import keep_quoted_claims, parse_claim_lines, claim_prompt
from store import add_record, latest_e_reviews_payload

REVIEW_KEEP = 50
INDEXED_SOURCES = frozenset({"pubmed", "epmc"})
TYPE_MARK = re.compile(
    r"\b(case report|editorial|letter|comment|news)\b", re.IGNORECASE
)


def _fold(text: str) -> str:
    return (text or "").casefold().replace("-", " ").replace("_", " ")


def keep_e_paper(paper, *, terms: list[str], indexed: bool) -> bool:
    title = paper.title or ""
    if TYPE_MARK.search(title):
        return False
    if indexed:
        return True
    blob = _fold(f"{title}\n{paper.text or ''}")
    return any(_fold(term) in blob for term in terms if len(term.strip()) >= 4)


def harvest_disease_reviews(
    *,
    mesh_id: str,
    as_of: date | None,
    clients,
    sources: list[str],
    keep: int = REVIEW_KEEP,
    store=None,
    complete=None,
    name: str = "",
):
    if store is not None:
        replay = latest_e_reviews_payload(store, mesh_id=mesh_id, as_of=as_of)
        if replay:
            papers = []
            for row in replay.get("papers") or []:
                published = row.get("published")
                papers.append(
                    Paper(
                        paper_id=str(row.get("paper_id") or ""),
                        title=str(row.get("title") or ""),
                        text=str(row.get("text") or ""),
                        published=date.fromisoformat(published) if published else None,
                        source=str(row.get("source") or ""),
                    )
                )
            return papers
    name = (name or "").strip() or clients.mesh_preferred_name(mesh_id)
    terms = [name]
    extra = getattr(clients, "mesh_entry_terms", None)
    if callable(extra):
        try:
            terms = list(dict.fromkeys([name, *extra(mesh_id)]))
        except (OSError, TimeoutError, URLError):
            terms = [name]
    groups = []
    by_source = {}
    queries = {}
    for source in sources:
        query = disease_review_query(name, source, mesh_id=mesh_id)
        queries[source] = query
        raw = clients.search_papers(query, as_of=as_of, source=source, keep=keep)
        kept = take_qualified(raw, as_of=as_of, keep=keep, can_page=True)
        by_source[source] = len(kept)
        groups.append(kept)
    papers = [
        p
        for p in merge_by_id(groups)
        if keep_e_paper(p, terms=terms, indexed=p.source in INDEXED_SOURCES)
    ]
    if store is not None:
        add_record(
            store,
            kind="e_reviews",
            payload={
                "mesh_id": mesh_id,
                "name": name,
                "terms": terms,
                "query": queries,
                "n": len(papers),
                "by_source": by_source,
                "papers": [
                    {
                        "paper_id": p.paper_id,
                        "title": p.title,
                        "text": p.text,
                        "published": p.published.isoformat() if p.published else None,
                        "source": p.source,
                    }
                    for p in papers
                ],
            },
            as_of=as_of,
        )
    if complete is not None:
        for paper in papers:
            body = paper.text or ""
            rows = keep_quoted_claims(
                parse_claim_lines(complete(claim_prompt(body))),
                body,
            )
            if store is not None:
                add_record(
                    store,
                    kind="f_claim",
                    payload={
                        "mesh_id": mesh_id,
                        "paper_id": paper.paper_id,
                        "claims": rows,
                    },
                    as_of=as_of,
                )
    return papers
