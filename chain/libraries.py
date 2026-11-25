"""Public library calls and paper helpers. Tests inject fakes; this file does not query on import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from http.client import IncompleteRead, RemoteDisconnected
import json
import os
from pathlib import Path
import re
import ssl
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

try:
    import certifi
    # ponytail: Windows default CA store fails HTTPS (expired chain path); certifi verifies
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass


@dataclass
class Paper:
    paper_id: str
    title: str
    text: str
    published: date | None
    source: str
    doi: str = ""
    pmid: str = ""
    bohrium_id: str = ""


def take_qualified(
    ranked: Iterable[Paper],
    *,
    as_of: date | None,
    keep: int = 50,
    can_page: bool = True,
    fetch_cap: int = 100,
) -> list[Paper]:
    kept: list[Paper] = []
    fetched = 0
    for paper in ranked:
        fetched += 1
        if not can_page and fetched > fetch_cap:
            break
        # ponytail: no day-level date is kept, including a year-only PubMed pubdate that may be after the freeze.
        # Upgrade: drop a year later than the freeze year.
        if as_of is not None and paper.published is not None and paper.published > as_of:
            continue
        kept.append(paper)
        if len(kept) >= keep:
            break
    return kept


def merge_by_id(groups: list[list[Paper]]) -> list[Paper]:
    by_id: dict[str, Paper] = {}
    for group in groups:
        for paper in group:
            by_id.setdefault(paper.paper_id, paper)
    return list(by_id.values())


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def publication_day(value: object) -> date | None:
    """Year, month, and day. A bare year or a month range is not a day."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    slash = text.replace("/", "-")
    if len(slash) >= 10 and slash[4] == "-" and slash[7] == "-":
        try:
            return date(int(slash[0:4]), int(slash[5:7]), int(slash[8:10]))
        except ValueError:
            return None
    parts = text.replace(",", " ").split()
    if len(parts) < 3 or not parts[0][:4].isdigit():
        return None
    month = _MONTHS.get(parts[1][:3].casefold())
    day_text = ""
    for char in parts[2]:
        if not char.isdigit():
            break
        day_text += char
    if not month or not day_text:
        return None
    try:
        return date(int(parts[0][:4]), month, int(day_text))
    except ValueError:
        return None


def _parse_day(value: object) -> date | None:
    if value is None:
        return None
    text = str(value).strip().replace("/", "-")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
        except ValueError:
            return None
    if len(text) >= 4 and text[:4].isdigit():
        try:
            return date(int(text[:4]), 1, 1)
        except ValueError:
            return None
    return None


def _http_body(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> bytes:
    req = Request(url, data=data, headers=headers or {"User-Agent": "Indicator3"})
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            last = exc
        except (
            URLError,
            TimeoutError,
            ConnectionResetError,
            RemoteDisconnected,
            IncompleteRead,
            ssl.SSLError,
        ) as exc:
            last = exc
        time.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last


def _http_json(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> object:
    return json.loads(_http_body(url, data=data, headers=headers, timeout=timeout).decode("utf-8"))


def epmc_search_params(query: str, *, as_of: date | None, page: int) -> dict[str, object]:
    q = query
    if as_of is not None:
        q = f"({query}) AND FIRST_PDATE:[* TO {as_of.isoformat()}]"
    return {"query": q, "format": "json", "pageSize": 50, "page": page}


def disease_review_query(name: str, source: str, mesh_id: str = "") -> str:
    name = name.strip()
    mesh_id = (mesh_id or "").strip()
    if source == "pubmed":
        if mesh_id:
            return f'"{name}"[Majr] AND review[pt]'
        return f"({name}) AND review[pt]"
    if source == "epmc":
        key = mesh_id or name
        return f'({key}) AND PUB_TYPE:"Review"'
    return name


def mesh_terms_query(mesh_id: str) -> str:
    """Labels of the preferred concept. Some records use term, others only preferredTerm."""
    uri = f"http://id.nlm.nih.gov/mesh/{mesh_id}"
    return (
        "PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "SELECT DISTINCT ?label WHERE {\n"
        f"  <{uri}> meshv:preferredConcept ?c .\n"
        "  {\n"
        "    ?c meshv:term ?t .\n"
        "    { ?t meshv:prefLabel ?label } UNION { ?t meshv:altLabel ?label }\n"
        "  }\n"
        "  UNION {\n"
        "    ?c meshv:preferredTerm ?t .\n"
        "    { ?t meshv:prefLabel ?label } UNION { ?t meshv:altLabel ?label }\n"
        "  }\n"
        "  UNION { ?c rdfs:label ?label }\n"
        "}"
    )


def mesh_term_labels(payload: object) -> list[str]:
    rows = ((payload or {}).get("results") or {}).get("bindings") or []
    out = []
    seen = set()
    for row in rows:
        val = str((row.get("label") or {}).get("value") or "").strip()
        key = val.casefold()
        if val and key not in seen:
            seen.add(key)
            out.append(val)
    return out


MESH_SPARQL = "https://id.nlm.nih.gov/mesh/sparql"


def mesh_preferred_query(mesh_id: str) -> str:
    return (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        f"SELECT ?label WHERE {{ <http://id.nlm.nih.gov/mesh/{mesh_id}> rdfs:label ?label }}"
    )


def mesh_label(payload: object) -> str:
    rows = ((payload or {}).get("results") or {}).get("bindings") or []
    for row in rows:
        val = str((row.get("label") or {}).get("value") or "").strip()
        if val:
            return val
    return ""


def mesh_tree_query(mesh_id: str) -> str:
    return (
        "PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#>\n"
        "SELECT ?tree WHERE {\n"
        f"  <http://id.nlm.nih.gov/mesh/{mesh_id}> meshv:treeNumber ?tree .\n"
        "}"
    )


def mesh_tree_numbers(payload: object) -> list[str]:
    rows = ((payload or {}).get("results") or {}).get("bindings") or []
    out = []
    seen = set()
    for row in rows:
        token = str((row.get("tree") or {}).get("value") or "").strip().split("/")[-1]
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def uniprot_one(results: object) -> dict | None:
    """One reviewed entry. Zero or several entries are not a protein."""
    rows = results if isinstance(results, list) else []
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    return rows[0]


def mesh_uid(payload: object) -> str:
    """One MeSH hit. Zero or several hits are not a concept."""
    result = (payload if isinstance(payload, dict) else {}).get("esearchresult") or {}
    ids = result.get("idlist") or []
    if str(result.get("count") or "") != "1" or len(ids) != 1:
        return ""
    return str(ids[0])


def mesh_summary_record(row: object) -> dict:
    if not isinstance(row, dict):
        return {}
    ui = str(row.get("ds_meshui") or "").strip()
    raw = row.get("ds_meshterms") or []
    if isinstance(raw, str):
        raw = [raw]
    forms = []
    seen = set()
    for term in raw:
        text = " ".join(str(term).split())
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            forms.append(text)
    if not ui:
        return {}
    return {"mesh": ui, "forms": forms}


def bohrium_cite_id(item: dict) -> str:
    """Identifier written for a Bohrium paper. A DOI is the report id; otherwise the number Bohrium sent."""
    doi = str(item.get("doi") or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I).strip()
    if doi.lower().startswith("10."):
        return doi
    return str(item.get("pmid") or item.get("paperId") or item.get("id") or "")


def bohrium_records(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("list") or data.get("records") or data.get("papers") or []
    else:
        raw = []
    return [item for item in raw if isinstance(item, dict)]


def uniprot_aliases(row: dict, query_name: str) -> list[str]:
    genes = []
    for gene in row.get("genes") or []:
        primary = gene.get("geneName", {}).get("value")
        if primary:
            genes.append(primary)
        for syn in gene.get("synonyms") or []:
            if syn.get("value"):
                genes.append(syn["value"])
    desc = row.get("proteinDescription") or {}
    rec = (desc.get("recommendedName") or {}).get("fullName", {}).get("value")
    extra = []
    for alt in desc.get("alternativeNames") or []:
        full = (alt.get("fullName") or {}).get("value")
        if full:
            extra.append(full)
        for short in alt.get("shortNames") or []:
            if short.get("value"):
                extra.append(short["value"])
    # The query string is not a record name. Callers add a scope name only after it matches.
    return list(dict.fromkeys(n for n in [*genes, rec or "", *extra] if n))


def summary_doi(row: dict) -> str:
    for item in row.get("articleids") or []:
        if str(item.get("idtype") or "").casefold() == "doi" and item.get("value"):
            return str(item["value"])
    loc = str(row.get("elocationid") or "")
    if loc.lower().startswith("doi:"):
        return loc.split(":", 1)[1].strip()
    return ""


class LiveClients:
    """Required public entries. Bohrium needs BOHRIUM_API_KEY."""

    def uniprot(self, name: str) -> dict:
        queries = (
            f"gene_exact:{name} AND organism_id:9606 AND reviewed:true",
            f"gene:{name} AND organism_id:9606 AND reviewed:true",
            f'protein_name:"{name}" AND organism_id:9606 AND reviewed:true',
            f"gene:{name} AND organism_id:9606",
        )
        if re.fullmatch(r"[A-NR-Z][0-9][A-Z0-9]{3}[0-9]", name):
            queries = (f"accession:{name}",) + queries
        row = None
        for query in queries:
            q = urlencode({"query": query, "format": "json", "size": 2})
            payload = _http_json(f"https://rest.uniprot.org/uniprotkb/search?{q}")
            row = uniprot_one(payload.get("results") or [])
            if row is not None:
                break
        if row is None:
            raise RuntimeError(f"UniProt returned no human gene for {name!r}")
        acc = row["primaryAccession"]
        aliases = uniprot_aliases(row, name)
        genes = [g.get("geneName", {}).get("value", "") for g in row.get("genes") or [] if g.get("geneName")]
        return {
            "name": genes[0] if genes else name,
            "uniprot_id": acc,
            "aliases": aliases,
        }

    def search_papers(
        self, query: str, *, as_of: date | None = None, source: str | None = None, keep: int = 50, strict: bool = False
    ) -> list[Paper]:
        try:
            if source == "pubmed":
                return self._pubmed(query, as_of, keep=keep, strict=strict)
            if source == "epmc":
                return self._epmc(query, as_of, keep=keep)
            if source == "bohrium":
                return self._bohrium(query, as_of, keep=keep)
        except HTTPError as exc:
            # ponytail: harvest turns a 5xx/429 into an empty source so the others still store.
            # strict=True is the link search: the caller must see source_unavailable, not zero hits.
            if exc.code < 500 and exc.code != 429:
                raise
            if strict:
                raise
            return []
        raise ValueError(source)

    def _pubmed(self, query: str, as_of: date | None, *, keep: int = 50, strict: bool = False) -> list[Paper]:
        papers: list[Paper] = []
        retstart = 0
        while len(papers) < keep:
            params = {
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "sort": "relevance",
                "retmax": 50,
                "retstart": retstart,
            }
            if as_of is not None:
                params["datetype"] = "pdat"
                params["mindate"] = "1800/01/01"
                params["maxdate"] = as_of.strftime("%Y/%m/%d")
            try:
                search = _http_json(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{urlencode(params)}")
                ids = (search.get("esearchresult") or {}).get("idlist") or []
                if not ids:
                    break
                summary = _http_json(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                    + urlencode({"db": "pubmed", "retmode": "json", "id": ",".join(ids)})
                )
                result = summary.get("result") or {}
                abstracts = self._pubmed_abstracts(ids)
            except HTTPError as exc:
                # ponytail: harvest keeps papers so far after a 5xx/429. A strict link search raises.
                if exc.code < 500 and exc.code != 429:
                    raise
                if strict:
                    raise
                break
            page: list[Paper] = []
            for pmid in ids:
                row = result.get(pmid) or {}
                published = (
                    publication_day(row.get("sortpubdate"))
                    or publication_day(row.get("epubdate"))
                    or publication_day(row.get("pubdate"))
                )
                title = str(row.get("title") or pmid)
                page.append(
                    Paper(pmid, title, abstracts.get(pmid) or "", published, "pubmed", summary_doi(row))
                )
            extra = take_qualified(page, as_of=as_of, keep=keep - len(papers), can_page=True)
            papers.extend(extra)
            if len(ids) < 50:
                break
            retstart += 50
        return papers[:keep]

    def _pubmed_abstracts(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(
            {"db": "pubmed", "rettype": "abstract", "retmode": "xml", "id": ",".join(ids)}
        )
        try:
            raw = _http_body(url, headers={"User-Agent": "Indicator3"})
        except HTTPError as exc:
            # ponytail: NCBI 400 on a 50-id XML batch leaves those abstracts empty. Upgrade: API key + 3 rps.
            if exc.code != 400:
                raise
            return {}
        root = ElementTree.fromstring(raw)
        out: dict[str, str] = {}
        for article in root.findall(".//PubmedArticle"):
            pmid_el = article.find(".//PMID")
            parts = [el.text or "" for el in article.findall(".//AbstractText")]
            if pmid_el is not None and pmid_el.text and any(parts):
                out[pmid_el.text] = " ".join(p for p in parts if p)
        return out

    def _epmc(self, query: str, as_of: date | None, *, keep: int = 50) -> list[Paper]:
        papers: list[Paper] = []
        page = 1
        while len(papers) < keep:
            params = urlencode(epmc_search_params(query, as_of=as_of, page=page))
            payload = _http_json(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{params}")
            hits = (payload.get("resultList") or {}).get("result") or []
            if not hits:
                break
            batch = []
            for row in hits:
                pmid = str(row.get("pmid") or row.get("id") or "")
                if not pmid:
                    continue
                published = _parse_day(row.get("firstPublicationDate") or row.get("pubYear"))
                text = str(row.get("abstractText") or "")
                batch.append(Paper(pmid, str(row.get("title") or pmid), text, published, "epmc"))
            extra = take_qualified(batch, as_of=as_of, keep=keep - len(papers), can_page=True)
            papers.extend(extra)
            if len(hits) < 50:
                break
            page += 1
        return papers[:keep]

    def _bohrium(
        self,
        query: str,
        as_of: date | None,
        *,
        keep: int | None = 50,
        words: list[str] | None = None,
    ) -> list[Paper]:
        token = os.environ.get("BOHRIUM_API_KEY")
        if not token:
            raise RuntimeError("Bohrium paper search needs BOHRIUM_API_KEY")
        papers: list[Paper] = []
        page = 1
        while keep is None or len(papers) < keep:
            payload = _http_json(
                "https://open.bohrium.com/openapi/v2/paper/rag/pass/keyword",
                data=json.dumps(
                    {
                        "words": list(words) if words else [query],
                        "question": query,
                        "type": 1,
                        "pageSize": 50,
                        "page": page,
                    }
                ).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "Indicator3",
                },
            )
            raw = bohrium_records(payload)
            if not raw:
                break
            batch = []
            for item in raw:
                title = str(item.get("enName") or item.get("title") or "")
                pid = bohrium_cite_id(item) or title
                published = _parse_day(
                    item.get("publishDate")
                    or item.get("coverDateStart")
                    or item.get("year")
                    or item.get("pubYear")
                )
                text = str(item.get("abstract") or item.get("enAbstract") or "")
                doi = bohrium_cite_id({"doi": item.get("doi") or ""})
                if not str(doi).lower().startswith("10."):
                    doi = ""
                batch.append(
                    Paper(
                        pid,
                        str(title),
                        text,
                        published,
                        "bohrium",
                        doi,
                        str(item.get("pmid") or ""),
                        str(item.get("paperId") or ""),
                    )
                )
            room = len(batch) if keep is None else keep - len(papers)
            extra = take_qualified(batch, as_of=as_of, keep=room, can_page=True)
            papers.extend(extra)
            if len(raw) < 50:
                break
            page += 1
        return papers if keep is None else papers[:keep]

    def mesh_preferred_name(self, mesh_id: str) -> str:
        q = urlencode(
            {"query": mesh_preferred_query(mesh_id), "format": "JSON"}
        )
        payload = _http_json(f"{MESH_SPARQL}?{q}")
        label = mesh_label(payload)
        if not label:
            raise RuntimeError(f"MeSH returned no preferred name for {mesh_id!r}")
        return label

    def mesh_entry_terms(self, mesh_id: str) -> list[str]:
        q = urlencode(
            {"query": mesh_terms_query(mesh_id), "format": "JSON"}
        )
        return mesh_term_labels(_http_json(f"{MESH_SPARQL}?{q}"))

    def mesh_trees(self, mesh_id: str) -> list[str]:
        ui = " ".join(str(mesh_id).split())
        if not ui:
            return []
        q = urlencode({"query": mesh_tree_query(ui), "format": "JSON"})
        return mesh_tree_numbers(_http_json(f"{MESH_SPARQL}?{q}"))

    def mesh_concept(self, name: str) -> dict:
        """The descriptor for this string when MeSH has exactly one hit. Failure is no concept."""
        text = " ".join(str(name).split())
        if not text:
            return {}
        try:
            params = {"db": "mesh", "term": f'"{text}"[MeSH Terms]', "retmode": "json", "retmax": 2}
            found = mesh_uid(_http_json(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urlencode(params)
            ))
            if not found:
                return {}
            summary = _http_json(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                + urlencode({"db": "mesh", "retmode": "json", "id": found})
            )
            return mesh_summary_record((summary.get("result") or {}).get(found) or {})
        except Exception:
            return {}


if __name__ == "__main__":
    assert bohrium_cite_id({"doi": "https://doi.org/10.1/ab", "paperId": "99"}) == "10.1/ab"
    assert bohrium_cite_id({"paperId": "817350575447343109", "pmid": "123"}) == "123"
    assert bohrium_cite_id({"paperId": "817350575447343109"}) == "817350575447343109"
    assert mesh_uid({"esearchresult": {"count": "1", "idlist": ["68002097"]}}) == "68002097"
    assert mesh_uid({"esearchresult": {"count": "2", "idlist": ["1", "2"]}}) == ""
    assert mesh_tree_numbers({"results": {"bindings": [{"tree": {"value": "C04.588"}}]}}) == ["C04.588"]
    assert uniprot_one([{"primaryAccession": "P12345"}, {"primaryAccession": "P99999"}]) is None
    assert uniprot_one([{"primaryAccession": "P12345"}])["primaryAccession"] == "P12345"
    assert "preferredTerm" in mesh_terms_query("D003324") and "meshv:term" in mesh_terms_query("D003324")
    assert mesh_summary_record({"ds_meshui": "D002097", "ds_meshterms": ["C-Reactive Protein"]})["mesh"] == "D002097"
    assert mesh_summary_record({"ds_meshterms": ["C-Reactive Protein"]}) == {}
    print("ok")
