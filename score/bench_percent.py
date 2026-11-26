"""Three percentages for the ordinary-model benchmark. No point total.

A sentence counts when its citation identifies one paper and the sentence is a
continuous span of that paper's abstract. A sentence that does not meet this
test stays in the denominator. The same sentences are scored for a publication
date on or before the freeze date.
"""

from __future__ import annotations

import html
import json
import re
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))

from assess import is_known, known_names, norm, parse_top5  # noqa: E402

ORD = BASE / "打分api普通模型" / "11.8新提示"
CACHE = HERE / "out" / "bench-cache"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
PMID_RE = re.compile(r"\b(?:pmid|pubmed)\s*[:#]?\s*(\d{5,9})\b", re.I)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

PAIRS = (
    ("kv1.3-igan", "2026-04-30", "known/Kv1.3.json",
     "nominate/data/Kv1.3/Results1/nominate/top5.md",
     "chain/Kv1-3/D005922-Glomerulonephritis-IGA/result-5/axes.json"),
    ("il17a-hidradenitis", "2016-07-11", "known/IL17A.json",
     "nominate/data/catalog2/IL17A/2016-07-11/nominate/top5.md",
     "chain/pairs/nominate-top5/IL17A-D017497/axes.json"),
    ("il4r-eoe", "2015-05-12", "known/IL4R-2015-05-12.json",
     "nominate/data/catalog2/IL4R/2015-05-12/nominate/top5.md",
     "chain/pairs/nominate-top5/IL4R-D057765/axes.json"),
    ("il4r-bullous", "2020-10-28", "known/IL4R-2020-10-28.json",
     "nominate/data/catalog2/IL4R/2020-10-28/nominate/top5.md",
     "chain/pairs/nominate-top5/IL4R-D010391/axes.json"),
    ("cfb-igan", "2018-02-07", "known/CFB.json",
     "nominate/data/catalog2/CFB/2018-02-07/nominate/top5.md",
     "chain/pairs/nominate-top5/CFB-D005922/axes.json"),
    ("c5-mg", "2008-10-01", "known/C5-2008-10-01.json",
     "nominate/data/catalog2/C5/2008-10-01/nominate/top5.md",
     "chain/pairs/nominate-top5/C5-D009157/axes.json"),
    ("c5-nmo", "2009-04-01", "known/C5-2009-04-01.json",
     "nominate/data/catalog2/C5/2009-04-01/nominate/top5.md",
     "chain/pairs/nominate-top5/C5-D009471/axes.json"),
    ("tslp-nasal-polyps", "2021-04-22", "known/TSLP.json",
     "nominate/data/catalog2/TSLP/2021-04-22/nominate/top5.md",
     "chain/pairs/nominate-top5/TSLP-D009298/axes.json"),
)


def fold(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\u00ad", "").replace("\u200b", "")
    for src, dst in (
        ("\u201c", '"'), ("\u201d", '"'), ("\u201e", '"'), ("\u00ab", '"'), ("\u00bb", '"'),
        ("\u2018", "'"), ("\u2019", "'"), ("\u201a", "'"), ("\u2032", "'"),
    ):
        text = text.replace(src, dst)
    return re.sub(r"\s+", " ", text).strip()


# ponytail: glyph fold only. A different word still fails the substring test.
assert fold("the \u201cfour-hit hypothesis\u201d") == fold('the "four-hit hypothesis"')


def on_or_before(pub: str, as_of: str) -> bool | None:
    """True when the date is on or before as_of. None when there is no date.

    A year alone is enough when that year is earlier than the freeze year.
    The same year without a month, or the same month without a day, does not
    show that the paper is on or before the freeze day.
    """
    pub = (pub or "").strip()
    if len(pub) < 4 or not pub[:4].isdigit():
        return None
    year = int(pub[:4])
    freeze = date.fromisoformat(as_of)
    if year < freeze.year:
        return True
    if year > freeze.year:
        return False
    parts = pub.split("-")
    if len(parts) < 2 or not parts[1].isdigit():
        return False
    month = int(parts[1])
    if month < 1 or month > 12:
        return False
    if month < freeze.month:
        return True
    if month > freeze.month:
        return False
    if len(parts) < 3 or not parts[2][:2].isdigit():
        return False
    day = int(parts[2][:2])
    return 1 <= day <= 31 and day <= freeze.day


def identifiers(source: str) -> tuple[str, str]:
    source = source or ""
    doi = ""
    found = DOI_RE.search(source)
    if found:
        doi = found.group(0).rstrip(").,;")
    pmid = ""
    labeled = PMID_RE.search(source)
    if labeled:
        pmid = labeled.group(1)
    else:
        url_pmid = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})", source, re.I)
        if url_pmid:
            pmid = url_pmid.group(1)
        elif not doi and re.fullmatch(r"\d{5,9}", source.strip()):
            pmid = source.strip()
    return doi, pmid


LOOKUP = "parts-3"
_TITLE_STOP = frozenset(
    "a an the and or of for in on to with from by via into over per its their this that".split()
)


def _ascii_cite(text: str) -> str:
    text = html.unescape(text or "").replace("’", "'").replace("‘", "'")
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    return text.replace("…", " ")


def _first_author(text: str) -> str:
    named = re.match(r"^([A-Z][A-Za-z'-]{2,})\s+et al\b", text)
    if named:
        return named.group(1)
    joined = re.match(r"^([A-Z][A-Za-z'-]{2,})(?:\s*(?:/|&)\s*[A-Z][A-Za-z'-]{2,})+", text)
    if joined:
        return joined.group(1)
    roster = re.match(
        r"^([A-Z][A-Za-z'-]{2,})(?:\s*,\s*[A-Z][A-Za-z'-]{2,})+"
        r"(?:\s*(?:&|/)\s*[A-Z][A-Za-z'-]{2,})*(?:\s+et al\.?)?\s*,",
        text,
    )
    if roster:
        return roster.group(1)
    initial = re.match(r"^([A-Z][a-z'-]{2,})\s+[A-Z](?:\.|,)", text)
    if initial:
        return initial.group(1)
    bare = re.match(r"^([A-Z][a-z'-]{2,})(?:\s+lab)?\s*,?\s*(?:19|20)\d{2}\b", text)
    if bare:
        return bare.group(1)
    return ""


def _journal_name(text: str, author: str) -> str:
    dotted = re.search(r"([A-Z][^.]{2,70}?)\.\s*(?:19|20)\d{2}\s*;\s*\d+", text)
    if dotted and 1 <= len(dotted.group(1).split()) <= 8:
        return " ".join(dotted.group(1).split())
    found = ""
    for match in re.finditer(
        r",\s*([A-Z][^,()]{2,60}?)\s*(?:\([^)]*\))?\s*,?\s*(?:19|20)\d{2}\b",
        text,
    ):
        chunk = " ".join(match.group(1).split())
        if 1 <= len(chunk.split()) <= 8 and "et al" not in chunk.casefold():
            found = chunk
    if found:
        return found
    leading = re.match(r"^([A-Z][^,]{2,40}?)\s*,\s*(?:19|20)\d{2}\b", text)
    if leading and 1 <= len(leading.group(1).split()) <= 8:
        chunk = " ".join(leading.group(1).split())
        if not author or chunk.split()[0].casefold() != author.casefold():
            return chunk
    return ""


def citation_queries(source: str) -> list[str]:
    """PubMed queries from whatever parts a citation actually contains.

    DOI, PMID, and PMCID are handled earlier. Here the usable pieces are the
    first author, journal, year, first page, and title words, including an
    author and a year with nothing else, and a title with no bibliographic
    line. A query is accepted only when the search returns exactly one paper.
    Journal and year alone are not a query.
    """
    text = _ascii_cite(source)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"^source:\s*", "", text, flags=re.I)
    text = re.split(r"\s+-\s+(?:excluded|note)\b", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\((?:as above|citing|pre-\d{4}|figure|note)[^)]*\)", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .;,")
    if not text:
        return []
    page = ""
    paged = re.search(r"\b((?:19|20)\d{2})\s*;\s*\d+\s*(?:\(\d+\))?\s*:\s*(\d{1,6})\b", text)
    years = re.findall(r"\b((?:19|20)\d{2})\b", text)
    year = paged.group(1) if paged else (years[0] if years else "")
    if paged:
        page = paged.group(2)
    author = _first_author(text)
    journal = _journal_name(text, author)
    if journal and author and journal.casefold() == author.casefold():
        journal = ""
    quoted = re.search(r'"([^"]{12,})"', text) or re.search(r"“([^”]{12,})”", text)
    paren = ""
    for item in re.findall(r"\(([^)]{8,})\)", text):
        if len(item.split()) >= 3 and not re.fullmatch(r"PMC\d+", item, re.I):
            paren = item
            break
    if quoted and len(quoted.group(1).split()) >= 3:
        title_source = quoted.group(1)
    else:
        title_source = paren or text
    for piece in (author, journal, *years):
        if piece:
            title_source = re.sub(rf"\b{re.escape(piece)}\b", " ", title_source, count=1)
    title_source = re.sub(r"\bet al\b", " ", title_source, flags=re.I)
    title_source = re.sub(r"\([^)]*\)", " ", title_source)
    words = []
    seen = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9'+-]{1,}", title_source):
        key = word.casefold()
        if key in seen or key in _TITLE_STOP or key in {"et", "al", "vol", "pp", "lab"}:
            continue
        seen.add(key)
        words.append(word)
    if not any(word[:1].isupper() for word in words):
        words = []
    words = words[:8]
    spans = []
    if len(words) >= 3:
        spans.append(words)
        if len(words) > 4:
            spans.append(words[:4])
    titles = [" ".join(f"{word}[Title]" for word in span) for span in spans]
    author_q = f"{author}[Author]" if author else ""
    year_q = f"{year}[pdat]" if year else ""
    page_q = f"{page}[pg]" if page else ""
    journal_q = f'"{journal}"[Journal]' if journal else ""
    queries = []

    def add(parts: list[str]) -> None:
        parts = [part for part in parts if part]
        if not parts:
            return
        query = " AND ".join(parts)
        if not any(tag in query for tag in ("[Author]", "[Title]", "[pg]")):
            return
        if query.count("[") == 1 and "[Author]" in query:
            return
        if query not in queries:
            queries.append(query)

    if journal_q or page_q:
        add([author_q, journal_q, year_q, page_q])
    if page and author_q:
        add([author_q, year_q, page_q])
    if page and journal_q:
        add([journal_q, year_q, page_q])
    if author_q and journal_q and year_q:
        add([author_q, journal_q, year_q])
    for title in titles:
        if journal_q and year_q:
            add([title, journal_q, year_q])
        if year_q:
            add([title, year_q])
        if author_q and year_q:
            add([author_q, title, year_q])
        if author_q:
            add([author_q, title])
        if journal_q:
            add([title, journal_q])
    if author_q and year_q:
        add([author_q, year_q])
    for extra in years:
        if extra != year and author_q:
            add([author_q, f"{extra}[pdat]"])
    for title in titles:
        add([title])
    return queries


def strip_wrap(text: str) -> str:
    text = (text or "").strip()
    for left, right in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if len(text) > 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text


def parse_nominate(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = re.match(r"\s*\d+\.\s+(.+?)\s*$", line)
        if not match:
            continue
        name = re.sub(r"[*_`]", "", match.group(1)).strip()
        name = re.sub(r"\s+\d+/\d+\s*$", "", name).strip()
        if name and name.casefold() != "disease name":
            rows.append({"name": name, "mesh": ""})
    if rows:
        return rows[:5]

    def take(mark: str, word_limit: int) -> list[str]:
        found = []
        blocked = {
            "framing", "target", "note", "ranking", "based", "below", "all",
            "want", "why", "cross", "proposed", "the", "new",
        }
        for line in text.splitlines():
            match = re.match(rf"^(.{{3,140}}?)\s*{re.escape(mark)}\s+\S", line.strip())
            if not match:
                continue
            name = re.sub(r"\s*\([^)]*\)", "", match.group(1)).strip(" -")
            words = name.split()
            if not words or len(words) > word_limit or "." in name:
                continue
            if words[0].casefold().strip(":") in blocked:
                continue
            found.append(name)
            if len(found) == 5:
                break
        return found

    found = take("—", 16) or take("–", 16) or take(":", 8)
    return [{"name": name, "mesh": ""} for name in found]


def _bare_quote(line: str) -> str:
    match = re.fullmatch(r"[\"“](.+)[\"”]", line.strip())
    if not match or len(match.group(1).strip()) < 40:
        return ""
    return match.group(1).strip()


def _sentence_source(line: str) -> tuple[str, str] | None:
    if "|" not in line:
        return None
    left, _, right = line.partition("|")
    left = re.sub(r"^Original abstract sentence:\s*", "", left.strip(), flags=re.I)
    right = right.strip()
    quoted = re.match(r"^[\"“](.{40,})[\"”]\s*(.*)$", right)
    if quoted:
        return quoted.group(1).strip(), quoted.group(2).strip()
    if left:
        return left, right
    return None


def parse_chain(text: str) -> list[dict]:
    rows = []
    path = 1
    step_n = 0
    current = None
    pending = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-").strip()
        if not line:
            continue
        if pending:
            if line.lower().startswith("source:"):
                if current is not None:
                    rows.append({**current, "sentence": pending, "source": line.split(":", 1)[1].strip()})
                pending = ""
                continue
            pending = ""
        if re.fullmatch(r"Path\s+\d+", line, re.I):
            path = int(re.search(r"\d+", line).group(0))
            current = None
            continue
        quoted = _bare_quote(line)
        if quoted and "|" not in line:
            pending = quoted
            continue
        piece = _sentence_source(line)
        if "→" not in line and "->" not in line:
            if current is not None and piece:
                sentence, source = piece
                rows.append({**current, "sentence": sentence, "source": source})
            continue
        arrow = "→" if "→" in line else "->"
        left, _, rest = line.partition(arrow)
        right, _, extra = rest.partition("|")
        step_n += 1
        current = {"path": path, "step": step_n, "left": left.strip(" |"), "right": right.strip()}
        if extra.strip():
            sentence, _, source = extra.partition("|")
            sentence = re.sub(r"^Original abstract sentence:\s*", "", sentence.strip(), flags=re.I)
            if sentence:
                rows.append({**current, "sentence": sentence, "source": source.strip()})
    return rows


_EVIDENCE = {"related", "denied", "co-occurrence"}


def _shown_sentence(row: dict) -> bool:
    """Same sentences the chain report lists. other is stored and not listed."""
    if "relation" not in row:
        return True
    return str(row.get("relation") or "") in _EVIDENCE


def _display_hops(axis: dict, joins: list) -> list:
    """The report continues an axis with the hop that joins its last node to the disease."""
    hops = list(axis.get("hops") or [])
    if not hops:
        return hops
    last = " ".join(str(hops[-1].get("right") or "").split()).casefold()
    for hop in joins or []:
        left = " ".join(str(hop.get("left") or "").split()).casefold()
        right = " ".join(str(hop.get("right") or "").split()).casefold()
        if left == last and right and right != last:
            hops.append(hop)
    return hops


def indichain_sentences(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    joins = payload.get("joins") or []
    rows = []
    step_n = 0
    for axis_n, axis in enumerate(payload.get("axes") or [], 1):
        for hop in _display_hops(axis, joins):
            step_n += 1
            for row in hop.get("sentences") or []:
                if not _shown_sentence(row):
                    continue
                sentence = str(row.get("sentence") or "").strip()
                if not sentence:
                    continue
                doi = str(row.get("doi") or "")
                pmid = str(row.get("pmid") or "")
                rows.append({
                    "path": axis_n,
                    "step": step_n,
                    "left": str(hop.get("left") or ""),
                    "right": str(hop.get("right") or ""),
                    "sentence": sentence,
                    "source": doi or pmid,
                    "doi": doi,
                    "pmid": pmid,
                })
    return rows


def _get(url: str, timeout: int = 40) -> bytes:
    last: BaseException | None = None
    for attempt in range(4):
        try:
            req = Request(url, headers={"User-Agent": "indicator-bench"})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except (URLError, TimeoutError, ConnectionResetError) as exc:
            last = exc
        time.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last


class Papers:
    def __init__(self) -> None:
        CACHE.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _cache(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:120]
        return CACHE / f"{safe}.json"

    def resolve(self, doi: str, pmid: str, source: str = "", want_full: bool = False) -> dict:
        pmc = re.search(r"PMC(\d+)", source or "", re.I) if not doi and not pmid else None
        if doi:
            key = f"doi-{doi}"
        elif pmid:
            key = f"pmid-{pmid}"
        elif pmc:
            key = f"pmc-{pmc.group(1)}"
        else:
            key = f"cite-{source}"
        path = self._cache(key)
        record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        stale = record is not None and not record.get("resolved") and record.get("lookup") != LOOKUP
        fetched = False
        if record is None or stale or (want_full and "pmcid" not in record):
            record = self._fetch(doi, pmid, source)
            fetched = True
        if want_full and record.get("resolved") and not record.get("full_tried"):
            pmcid = str(record.get("pmcid") or "")
            record["full"] = self._full(pmcid) if pmcid else ""
            record["open"] = bool(record["full"])
            record["full_tried"] = True
            fetched = True
        if fetched or not path.is_file():
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return record

    def _fetch(self, doi: str, pmid: str, source: str) -> dict:
        record = {
            "doi": doi, "pmid": pmid, "resolved": False, "date": "",
            "abstract": "", "full": "", "open": False, "pmcid": "", "full_tried": False,
            "lookup": LOOKUP,
        }
        if not doi and not pmid:
            pmid = self._pmid_for_citation(source)
            record["pmid"] = pmid
        hit = self._epmc(doi, pmid)
        if not hit and not doi and not pmid:
            hit = self._epmc_pmc(source)
            if hit:
                record["pmid"] = str(hit.get("pmid") or "")
        if not hit and doi:
            pmid = self._pmid_for_doi(doi)
            if pmid:
                record["pmid"] = pmid
                hit = self._epmc("", pmid)
        if not hit:
            return record
        record["resolved"] = True
        record["doi"] = hit.get("doi") or doi
        record["pmid"] = str(hit.get("pmid") or pmid or "")
        record["date"] = str(hit.get("firstPublicationDate") or hit.get("pubYear") or "")
        record["abstract"] = fold(str(hit.get("abstractText") or ""))
        record["pmcid"] = str(hit.get("pmcid") or "")
        return record

    def _epmc(self, doi: str, pmid: str) -> dict | None:
        if doi:
            query = f'DOI:"{doi}"'
        elif pmid:
            query = f"EXT_ID:{pmid} AND SRC:MED"
        else:
            return None
        url = f"{EPMC}?query={quote(query)}&format=json&resultType=core&pageSize=3"
        try:
            payload = json.loads(_get(url).decode("utf-8", "replace"))
        except Exception:
            return None
        hits = ((payload.get("resultList") or {}).get("result") or [])
        if doi:
            for hit in hits:
                if str(hit.get("doi") or "").casefold() == doi.casefold():
                    return hit
        return hits[0] if hits else None

    def _epmc_pmc(self, source: str) -> dict | None:
        match = re.search(r"PMC(\d+)", source or "", re.I)
        if not match:
            return None
        url = f"{EPMC}?query={quote('PMCID:PMC' + match.group(1))}&format=json&resultType=core&pageSize=1"
        try:
            payload = json.loads(_get(url).decode("utf-8", "replace"))
        except Exception:
            return None
        hits = ((payload.get("resultList") or {}).get("result") or [])
        return hits[0] if hits else None

    def _pmid_for_citation(self, source: str) -> str:
        for query in citation_queries(source):
            url = f"{NCBI}/esearch.fcgi?db=pubmed&term={quote(query)}&retmode=json"
            try:
                payload = json.loads(_get(url).decode("utf-8", "replace"))
            except Exception:
                continue
            ids = ((payload.get("esearchresult") or {}).get("idlist") or [])
            if len(ids) == 1:
                return str(ids[0])
        return ""

    def _pmid_for_doi(self, doi: str) -> str:
        url = f"{NCBI}/esearch.fcgi?db=pubmed&term={quote(doi + '[aid]')}&retmode=json"
        try:
            payload = json.loads(_get(url).decode("utf-8", "replace"))
        except Exception:
            return ""
        ids = ((payload.get("esearchresult") or {}).get("idlist") or [])
        return str(ids[0]) if ids else ""

    def _full(self, pmcid: str) -> str:
        pmcid = pmcid if pmcid.upper().startswith("PMC") else "PMC" + pmcid
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
        try:
            raw = _get(url, timeout=60).decode("utf-8", "replace")
        except Exception:
            return ""
        text = re.sub(r"<[^>]+>", " ", raw)
        return fold(text)


def novelty(rows: list[dict], known: dict) -> dict:
    names = known_names(known)
    slots = list(rows[:5])
    while len(slots) < 5:
        slots.append({"name": "", "mesh": "", "empty": True})
    flags = []
    seen = set()
    for row in slots:
        if row.get("empty") or not row.get("name"):
            flags.append(0)
            continue
        key = norm(row["name"])
        if key in seen:
            flags.append(0)
            continue
        seen.add(key)
        flags.append(0 if is_known(row, names) else 1)
    return {"new": sum(flags), "of": 5, "names": [row.get("name") or "" for row in slots], "flags": flags}


def judge_sentence(row: dict, paper: dict) -> str:
    """in, absent, or unresolved. Both sides are checked against the abstract."""
    if not paper.get("resolved"):
        return "unresolved"
    needle = fold(strip_wrap(row["sentence"]))
    if needle and needle in paper.get("abstract", ""):
        return "in"
    return "absent"


def percent(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return round(100 * num / den, 1)


def _bar(svg: list[str], x: int, y: int, h: int, value: float | None, color: str, label: str) -> None:
    height = 0 if value is None else int(h * value / 100)
    top = y + h - height
    svg.append(f'<rect x="{x}" y="{top}" width="28" height="{height}" fill="{color}"/>')
    shown = "—" if value is None else f"{value:.0f}"
    svg.append(f'<text x="{x + 14}" y="{top - 4}" text-anchor="middle" font-size="11">{shown}</text>')
    svg.append(
        f'<text x="{x + 14}" y="{y + h + 14}" text-anchor="middle" font-size="10">{html.escape(label)}</text>'
    )


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _mean(rows: list[dict], side: str, key: str) -> float | None:
    """Unweighted mean of pair percentages. Kv1.3–IgA nephropathy is omitted."""
    values = [
        row[side][key]
        for row in rows
        if row["slug"] != "kv1.3-igan" and row[side].get(key) is not None
    ]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def write_html(path: Path, rows: list[dict], label: str = "deepseek-v4-pro") -> None:
    charts = (
        ("Absent from the known set", "novel"),
        ("Verbatim in the cited abstract", "sentence"),
        ("Published on or before the freeze date", "freeze"),
    )
    sources = (
        ("indichain", "IndiChain", "#3d6b4f"),
        ("ordinary", label, "#b86b3a"),
    )
    width = 560
    blocks = []
    for title, key in charts:
        svg = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="300">',
            '<rect width="100%" height="100%" fill="white"/>',
            f'<text x="16" y="24" font-size="16">{title} (%)</text>',
            '<text x="16" y="288" font-size="12">Source</text>',
        ]
        for i, (side, label, color) in enumerate(sources):
            x = 150 + i * 160
            value = _mean(rows, side, key)
            height = 0 if value is None else int(190 * value / 100)
            top = 48 + 190 - height
            svg.append(f'<rect x="{x}" y="{top}" width="72" height="{height}" fill="{color}"/>')
            shown = "—" if value is None else f"{value:.1f}"
            svg.append(f'<text x="{x + 36}" y="{top - 6}" text-anchor="middle" font-size="13">{shown}</text>')
            svg.append(
                f'<text x="{x + 36}" y="262" text-anchor="middle" font-size="13">{html.escape(label)}</text>'
            )
        svg.append("</svg>")
        blocks.append("".join(svg))
    note = "<p>Each bar is the arithmetic mean of the percentages for the seven pairs.</p>"
    body = "\n".join(f"<p>{block}</p>" for block in blocks) + "\n" + note
    path.write_text(
        "<!DOCTYPE html><meta charset='utf-8'><title>benchmark</title>"
        "<style>body{font-family:sans-serif} svg{display:block}</style>"
        f"{body}\n",
        encoding="utf-8",
    )


def _xml(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_docx(path: Path, parked: list[dict]) -> None:
    paragraphs = ["Full texts that did not open. Number them before upload. Until then, these sentences are not counted as verbatim in the cited abstract."]
    if not parked:
        paragraphs.append("No full text failed to open in this run.")
    for row in parked:
        paragraphs.append(
            f"{row['n']}. {row['slug']}  path {row['path']}  step {row['step']}  "
            f"{row['left']} → {row['right']}"
        )
        paragraphs.append(f"Sentence: {row['sentence']}")
        paragraphs.append(f"Source: {row['source']}")
    body = []
    for paragraph in paragraphs:
        body.append(f"<w:p><w:r><w:t xml:space=\"preserve\">{_xml(paragraph)}</w:t></w:r></w:p>")
    document = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )
    rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/>"
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)


# Preferred name or alias, already passed through norm. A nomination hits when one of
# these phrases is the whole name or a continuous span inside it. TSLP counts both
# Nasal Polyps and Rhinosinusitis, the two descriptors used for that indication.
TARGET_ALIASES = {
    "il17a-hidradenitis": ("hidradenitis suppurativa", "acne inversa"),
    "il4r-eoe": ("eosinophilic esophagitis", "eosinophilic oesophagitis"),
    "il4r-bullous": ("bullous pemphigoid", "pemphigoid bullous"),
    "cfb-igan": ("glomerulonephritis iga", "iga nephropathy", "immunoglobulin a nephropathy"),
    "c5-mg": ("myasthenia gravis",),
    "c5-nmo": ("neuromyelitis optica", "devic disease", "devic s disease"),
    "tslp-nasal-polyps": ("nasal polyps", "nasal polyposis", "rhinosinusitis"),
}


def hits_target(slug: str, names: list[str]) -> bool:
    aliases = TARGET_ALIASES.get(slug) or ()
    for name in names:
        folded = f" {norm(name)} "
        if any(f" {alias} " in folded for alias in aliases):
            return True
    return False


PAIR_LABEL = {
    "kv1.3-igan": "Kv1.3 — IgA nephropathy",
    "il17a-hidradenitis": "IL17A — Hidradenitis Suppurativa",
    "il4r-eoe": "IL4R — Eosinophilic Esophagitis",
    "il4r-bullous": "IL4R — Pemphigoid, Bullous",
    "cfb-igan": "CFB — Glomerulonephritis, IGA",
    "c5-mg": "C5 — Myasthenia Gravis",
    "c5-nmo": "C5 — Neuromyelitis Optica",
    "tslp-nasal-polyps": "TSLP — Nasal Polyps",
}


def _marked_names(names: list[str], flags: list[int]) -> str:
    parts = []
    for name, flag in zip(names, flags):
        if not name:
            continue
        parts.append(f"{name} ({'new' if flag else 'known'})")
    return "".join(parts)


def _chart(title: str, indi: float | None, other: float | None, other_label: str) -> bytes:
    import io
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(5.6, 3.4), dpi=140)
    labels = ["IndiChain", other_label]
    values = [0 if indi is None else indi, 0 if other is None else other]
    bars = ax.bar(labels, values, color=["#3d6b4f", "#b86b3a"], width=0.55)
    for bar, value in zip(bars, (indi, other)):
        shown = "—" if value is None else f"{value:.1f}"
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5, shown, ha="center", va="bottom")
    ax.set_ylim(0, 110)
    ax.set_ylabel("Percent")
    ax.set_title(title)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def write_report_docx(path: Path, rows: list[dict], label: str) -> None:
    import io
    from docx import Document
    from docx.shared import Inches, Pt

    shown = [row for row in rows if row["slug"] != "kv1.3-igan"] + [
        row for row in rows if row["slug"] == "kv1.3-igan"
    ]
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(10.5)
    doc.add_heading("Percentages for the seven pairs", level=1)
    doc.add_paragraph(
        "The horizontal axis is the source and the vertical axis is the percentage. "
        "Each bar is the arithmetic mean of the seven pairs, not a pooled count of sentences. "
        "Kv1.3 — IgA nephropathy has no approved drug, is omitted from the bars, and is the last row of each table. "
        f"Sources: IndiChain and {label}. Plotted on 8 November 2026."
    )
    definitions = (
        ("Absent from the known set", "novel", "Of the five disease names, the percentage absent from that target's known-indication set at the freeze date. Empty names and duplicates are not counted as new."),
        ("Verbatim in the cited abstract", "sentence", "Of the sentences stored on the chains, the percentage whose citation identifies one paper and that are a continuous span of that paper's abstract. A sentence that does not meet this test remains in the denominator."),
        ("Published on or before the freeze date", "freeze", "Of the same sentences, the percentage whose citation identifies one paper published on or before the pair's freeze date. A sentence that does not meet this test remains in the denominator."),
    )
    for title, key, note in definitions:
        doc.add_heading(title, level=2)
        doc.add_paragraph(note)
        picture = io.BytesIO(_chart(title, _mean(rows, "indichain", key), _mean(rows, "ordinary", key), label))
        doc.add_picture(picture, width=Inches(5.6))
    doc.add_heading("Values for each pair", level=2)
    doc.add_paragraph("The last row is the arithmetic mean of the seven pairs, the same mean as the bars. Count columns are left blank on the mean row so that counts are not pooled.")

    doc.add_heading("Absent from the known set", level=3)
    table = doc.add_table(rows=1, cols=9)
    table.style = "Table Grid"
    for i, head in enumerate(("Pair", "Freeze date", "In the figure", "IndiChain", "%", "IndiChain five names", label, "%", f"{label} five names")):
        table.rows[0].cells[i].text = head
    for row in shown:
        cells = table.add_row().cells
        indi, other = row["indichain"], row["ordinary"]
        cells[0].text = PAIR_LABEL[row["slug"]] + ("" if row["slug"] != "kv1.3-igan" else " omitted from the bars")
        cells[1].text = row["as_of"]
        cells[2].text = "No" if row["slug"] == "kv1.3-igan" else "Yes"
        cells[3].text = f"{indi['novel_new']}/5"
        cells[4].text = f"{indi['novel']:.1f}%"
        cells[5].text = _marked_names(indi["names"], indi["flags"])
        cells[6].text = f"{other['novel_new']}/5"
        cells[7].text = f"{other['novel']:.1f}%"
        cells[8].text = _marked_names(other["names"], other["flags"])
    mean = table.add_row().cells
    mean[0].text = "Mean of the seven pairs"
    mean[4].text = _pct(_mean(rows, "indichain", "novel"))
    mean[7].text = _pct(_mean(rows, "ordinary", "novel"))

    doc.add_heading("Verbatim in the cited abstract", level=3)
    table = doc.add_table(rows=1, cols=11)
    table.style = "Table Grid"
    heads = (
        "Pair", "Freeze date", "In the figure",
        "IndiChain in the abstract", "%", "IndiChain unresolved", "IndiChain not in the abstract",
        f"{label} in the abstract", "%", f"{label} unresolved", f"{label} not in the abstract",
    )
    for i, head in enumerate(heads):
        table.rows[0].cells[i].text = head
    for row in shown:
        cells = table.add_row().cells
        indi, other = row["indichain"], row["ordinary"]
        cells[0].text = PAIR_LABEL[row["slug"]]
        cells[1].text = row["as_of"]
        cells[2].text = "No" if row["slug"] == "kv1.3-igan" else "Yes"
        cells[3].text = f"{indi['sentence_in']}/{indi['sentence_den']}"
        cells[4].text = _pct(indi["sentence"])
        cells[5].text = str(indi["unresolved"])
        cells[6].text = str(indi["absent"])
        cells[7].text = f"{other['sentence_in']}/{other['sentence_den']}"
        cells[8].text = _pct(other["sentence"])
        cells[9].text = str(other["unresolved"])
        cells[10].text = str(other["absent"])
    mean = table.add_row().cells
    mean[0].text = "Mean of the seven pairs"
    mean[4].text = _pct(_mean(rows, "indichain", "sentence"))
    mean[8].text = _pct(_mean(rows, "ordinary", "sentence"))

    doc.add_heading("Published on or before the freeze date", level=3)
    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    for i, head in enumerate(("Pair", "Freeze date", "In the figure", "IndiChain on or before the freeze date", "%", f"{label} on or before the freeze date", "%")):
        table.rows[0].cells[i].text = head
    for row in shown:
        cells = table.add_row().cells
        indi, other = row["indichain"], row["ordinary"]
        cells[0].text = PAIR_LABEL[row["slug"]]
        cells[1].text = row["as_of"]
        cells[2].text = "No" if row["slug"] == "kv1.3-igan" else "Yes"
        cells[3].text = f"{indi['freeze_ok']}/{indi['freeze_den']}"
        cells[4].text = _pct(indi["freeze"])
        cells[5].text = f"{other['freeze_ok']}/{other['freeze_den']}"
        cells[6].text = _pct(other["freeze"])
    mean = table.add_row().cells
    mean[0].text = "Mean of the seven pairs"
    mean[4].text = _pct(_mean(rows, "indichain", "freeze"))
    mean[6].text = _pct(_mean(rows, "ordinary", "freeze"))
    doc.add_paragraph("Unresolved: the source did not match one record. Not in the abstract: the record was resolved, but the sentence is not a continuous span of that abstract.")
    doc.save(path)


OVERVIEW = (
    ("IndiChain", "11.8deepseek", "indichain"),
    ("ChatGPT", "11.8chatgpt", "ordinary"),
    ("Qwen", "11.8qwen", "ordinary"),
    ("DeepSeek", "11.8deepseek", "ordinary"),
    ("kimi-k3", "11.8kimi-k3", "ordinary"),
    ("glm-5.3", "11.8glm-5.3", "ordinary"),
)


def write_overview_docx(path: Path) -> None:
    """One short table of the seven-pair means, plus the Kv1.3 pair on its own."""
    from docx import Document
    from docx.shared import Pt

    root = BASE / "打分api普通模型"
    loaded = []
    for name, folder, side in OVERVIEW:
        rows = json.loads((root / folder / "percent.json").read_text(encoding="utf-8"))
        loaded.append((name, side, rows))
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(10.5)
    doc.add_heading("Three percentages for the unaided models", level=1)
    doc.add_paragraph(
        "The table is the arithmetic mean of the seven pairs, the same mean as the three bars. "
        "Kv1.3 — IgA nephropathy has no approved drug, is omitted from the mean, and is listed on its own below. "
        "Plotted on 8 November 2026."
    )
    headers = ("Source", "Absent from the known set", "Verbatim in the cited abstract", "Published on or before the freeze date")
    keys = ("novel", "sentence", "freeze")
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for i, head in enumerate(headers):
        table.rows[0].cells[i].text = head
    for name, side, rows in loaded:
        cells = table.add_row().cells
        cells[0].text = name
        for i, key in enumerate(keys, start=1):
            cells[i].text = _pct(_mean(rows, side, key))
    doc.add_heading("Kv1.3 — IgA nephropathy", level=2)
    doc.add_paragraph("This pair is omitted from the mean above and from the bars. Freeze date 30 April 2026.")
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for i, head in enumerate(headers):
        table.rows[0].cells[i].text = head
    for name, side, rows in loaded:
        row = next(item for item in rows if item["slug"] == "kv1.3-igan")
        cells = table.add_row().cells
        cells[0].text = name
        side_row = row[side]
        for i, key in enumerate(keys, start=1):
            cells[i].text = _pct(side_row[key])
        if side_row["sentence_den"] == 0:
            cells[2].text = "no sentence to check"
            cells[3].text = "no sentence to check"
    doc.save(path)


def score_folder(folder: Path, label: str) -> None:
    """Score one ordinary-model folder against the saved IndiChain baseline."""
    baseline = {
        row["slug"]: row
        for row in json.loads((BASE / "打分api普通模型" / "11.8deepseek" / "percent.json").read_text(encoding="utf-8"))
    }
    papers = Papers()
    summary = []
    for slug, as_of, known_rel, _top5, _axes in PAIRS:
        known = json.loads((HERE / known_rel).read_text(encoding="utf-8"))
        names = parse_nominate((folder / slug / "nominate.txt").read_text(encoding="utf-8"))
        if len(names) != 5:
            raise SystemExit(f"{label} {slug} nominate parsed {len(names)}: {[row['name'] for row in names]}")
        ord_novel = novelty(names, known)
        ord_sent = parse_chain((folder / slug / "chain.txt").read_text(encoding="utf-8"))
        if not ord_sent:
            print(f"{label} {slug} chain parsed 0 sentences", flush=True)
        unique = {}
        for row in ord_sent:
            doi, pmid = identifiers(row["source"])
            row["doi"], row["pmid"] = doi, pmid
            key = (doi, pmid, "" if doi or pmid else row.get("source") or "")
            row["_key"] = key
            unique[key] = (doi, pmid, key[2])
        resolved = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(papers.resolve, doi, pmid, source): (doi, pmid, source)
                for doi, pmid, source in unique.values()
                if doi or pmid or source
            }
            done = 0
            for fut in as_completed(futures):
                resolved[futures[fut]] = fut.result()
                done += 1
                if done % 25 == 0 or done == len(futures):
                    print(f"  {label} {slug} papers {done}/{len(futures)}", flush=True)
        counts = {"in": 0, "den": 0, "unresolved": 0, "absent": 0, "freeze_ok": 0, "freeze_den": 0}
        for row in ord_sent:
            paper = resolved.get(row["_key"], {"resolved": False, "date": ""})
            mark = judge_sentence(row, paper)
            counts["den"] += 1
            counts["in"] += mark == "in"
            counts["unresolved"] += mark == "unresolved"
            counts["absent"] += mark == "absent"
            counts["freeze_den"] += 1
            verdict = on_or_before(paper.get("date") or "", as_of) if paper.get("resolved") else None
            counts["freeze_ok"] += verdict is True
        indi = baseline[slug]["indichain"]
        summary.append({
            "slug": slug,
            "as_of": as_of,
            "indichain": indi,
            "ordinary": {
                "novel": percent(ord_novel["new"], ord_novel["of"]),
                "novel_new": ord_novel["new"],
                "names": ord_novel["names"],
                "flags": ord_novel["flags"],
                "sentence": percent(counts["in"], counts["den"]),
                "sentence_in": counts["in"],
                "sentence_den": counts["den"],
                "unresolved": counts["unresolved"],
                "absent": counts["absent"],
                "freeze": percent(counts["freeze_ok"], counts["freeze_den"]),
                "freeze_ok": counts["freeze_ok"],
                "freeze_den": counts["freeze_den"],
                "sentences": len(ord_sent),
            },
        })
        print(
            f"{label} {slug} new {indi['novel_new']}/5 vs {ord_novel['new']}/5 "
            f"sentence {counts['in']}/{counts['den']} unresolved {counts['unresolved']} "
            f"freeze {counts['freeze_ok']}/{counts['freeze_den']}",
            flush=True,
        )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "percent.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_html(folder / "percent.html", summary, label)
    write_report_docx(folder / "three-metric-percentages.docx", summary, label)


def _check() -> None:
    assert on_or_before("2010", "2016-07-11") is True
    assert on_or_before("2016", "2016-07-11") is False
    assert on_or_before("2016-07", "2016-07-11") is False
    assert on_or_before("2016-07-11", "2016-07-11") is True
    assert on_or_before("2016-07-12", "2016-07-11") is False
    assert on_or_before("", "2016-07-11") is None
    text = "Path 1\nIL17A → TNF\nOne sentence here. | 10.1000/abc\nAnother sentence. | PMID 1234567\n"
    rows = parse_chain(text)
    assert len(rows) == 2
    assert rows[0]["left"] == "IL17A" and rows[0]["right"] == "TNF"
    assert identifiers(rows[0]["source"])[0] == "10.1000/abc"
    assert identifiers(rows[1]["source"])[1] == "1234567"
    assert identifiers("Ann Neurol 1987;19:112") == ("", "")
    page_query = citation_queries(
        "Beeton C, Wulff H. Proc Natl Acad Sci U S A. 2006;103(48):17414-17419."
    )
    assert page_query and "17414[pg]" in page_query[0]
    loose_query = citation_queries("Nelms et al., Annu Rev Immunol 1999")
    assert loose_query and "Nelms[Author]" in loose_query[0] and "1999[pdat]" in loose_query[0]
    assert "et al" not in loose_query[0]
    titled = citation_queries(
        "IL-17 induces monocyte migration in rheumatoid arthritis, J Immunol 2009"
    )
    assert any("monocyte[Title]" in query and "2009[pdat]" in query for query in titled)
    assert all("IL-17[Author]" not in query for query in titled)
    roster = citation_queries("Sahashi, Engel, Lambert & Howard,J Neuropathol Exp Neurol, 1980")
    assert roster and "Sahashi[Author]" in roster[0] and "Neuropathol" in roster[0] and "1980[pdat]" in roster[0]
    short = citation_queries("Sahashi et al., 1980")
    assert short and "Sahashi[Author]" in short[0] and "1980[pdat]" in short[0]
    pair = citation_queries("Guo & Ward, 2005")
    assert pair and "Guo[Author]" in pair[0] and "2005[pdat]" in pair[0]
    titled_only = citation_queries("The pathogenetic significance of C5b-9 in IgA nephropathy")
    assert any("nephropathy[Title]" in query for query in titled_only)
    assert citation_queries("J Exp Med, 1978") == []
    assert citation_queries("no citation here") == []
    assert identifiers("PubMed 20810612 (Kv1.3 blocker in anti-GBM GN)")[1] == "20810612"
    assert hits_target("cfb-igan", ["IgA Nephropathy"])
    assert hits_target("il4r-bullous", ["Bullous Pemphigoid"])
    assert hits_target("tslp-nasal-polyps", ["Chronic Rhinosinusitis with Nasal Polyps"])
    assert not hits_target("il4r-eoe", ["Chronic Rhinosinusitis with Nasal Polyps"])
    assert hits_target("c5-mg", ["Refractory acetylcholine receptor antibody-positive generalized myasthenia gravis"])
    assert not hits_target("il4r-eoe", ["Eosinophilic gastroenteritis"])
    assert not hits_target("il17a-hidradenitis", ["Multiple Sclerosis"])
    assert strip_wrap("“Kv1.3 is expressed.”") == "Kv1.3 is expressed."
    names = parse_nominate("1. Asthma\n2. Disease name\n3. Psoriasis\n")
    assert [row["name"] for row in names] == ["Asthma", "Psoriasis"]
    dashed = parse_nominate("Intro line\n\nAsthma — already tested\nEosinophilic Esophagitis (EoE) — next\n")
    assert [row["name"] for row in dashed] == ["Asthma", "Eosinophilic Esophagitis"]
    swapped = parse_chain(
        "C5 → C5a\n"
        'A paraphrase of the step. | "The small anaphylatoxin C5a induces a variety of biological responses upon binding." (Lee, 2008)\n'
    )
    assert swapped[0]["sentence"].startswith("The small anaphylatoxin")
    bare = parse_chain(
        "CFB → C3\n"
        "“During convertase assembly, factor B associates with C3b and magnesium forming a pro-convertase that is cleaved.”\n"
        "Source: Paper (2011). https://pubmed.ncbi.nlm.nih.gov/19136636/\n"
    )
    assert len(bare) == 1
    assert identifiers(bare[0]["source"])[1] == "19136636"
    assert identifiers("see https://pubmed.ncbi.nlm.nih.gov/39870185/?utm_source=chatgpt.com")[1] == "39870185"
    shown = {
        "axes": [{"hops": [
            {"left": "A", "right": "B", "sentences": [
                {"sentence": "kept", "relation": "related", "doi": "10.1/kept"},
                {"sentence": "dropped", "relation": "other"},
            ]},
        ]}],
        "joins": [{"left": "B", "right": "Disease", "sentences": [
            {"sentence": "joined", "relation": "co-occurrence", "pmid": "1"},
        ]}],
        "incomplete": [{"nodes": ["A"], "steps": [{"sentences": [{"sentence": "not on the report", "relation": "promotes"}]}]}],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(shown, handle)
        shown_path = Path(handle.name)
    listed = indichain_sentences(shown_path)
    shown_path.unlink()
    assert [row["sentence"] for row in listed] == ["kept", "joined"]
    print("check ok")


def main() -> None:
    papers = Papers()
    summary = []
    for slug, as_of, known_rel, top5_rel, axes_rel in PAIRS:
        known = json.loads((HERE / known_rel).read_text(encoding="utf-8"))
        indi_rows = parse_top5((BASE / top5_rel).read_text(encoding="utf-8"))
        ordinary_nom = (ORD / slug / "nominate.txt").read_text(encoding="utf-8")
        indi_novel = novelty(indi_rows, known)
        ord_novel = novelty(parse_nominate(ordinary_nom), known)
        indi_sent = indichain_sentences(BASE / axes_rel)
        ord_sent = parse_chain((ORD / slug / "chain.txt").read_text(encoding="utf-8"))
        jobs = [("indichain", row) for row in indi_sent] + [("ordinary", row) for row in ord_sent]
        unique = {}
        for _side, row in jobs:
            doi, pmid = (row.get("doi", ""), row.get("pmid", "")) if _side == "indichain" else identifiers(row["source"])
            row["doi"], row["pmid"] = doi, pmid
            key = (doi, pmid, "" if doi or pmid else (row.get("source") or ""))
            row["_key"] = key
            unique[key] = (doi, pmid, key[2])
        resolved = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(papers.resolve, doi, pmid, source): (doi, pmid, source)
                for (doi, pmid, source) in unique.values()
                if doi or pmid or source
            }
            done = 0
            for fut in as_completed(futures):
                resolved[futures[fut]] = fut.result()
                done += 1
                if done % 50 == 0 or done == len(futures):
                    print(f"  {slug} papers {done}/{len(futures)}", flush=True)
        counts = {
            side: {"in": 0, "den": 0, "unresolved": 0, "absent": 0, "freeze_ok": 0, "freeze_den": 0}
            for side in ("indichain", "ordinary")
        }
        for side, row in jobs:
            paper = resolved.get(row["_key"], {"resolved": False, "date": ""})
            mark = judge_sentence(row, paper)
            counts[side]["den"] += 1
            counts[side]["in"] += mark == "in"
            counts[side]["unresolved"] += mark == "unresolved"
            counts[side]["absent"] += mark == "absent"
            counts[side]["freeze_den"] += 1
            verdict = on_or_before(paper.get("date") or "", as_of) if paper.get("resolved") else None
            counts[side]["freeze_ok"] += verdict is True
        summary.append({
            "slug": slug,
            "as_of": as_of,
            "indichain": {
                "novel": percent(indi_novel["new"], indi_novel["of"]),
                "novel_new": indi_novel["new"],
                "names": indi_novel["names"],
                "flags": indi_novel["flags"],
                "sentence": percent(counts["indichain"]["in"], counts["indichain"]["den"]),
                "sentence_in": counts["indichain"]["in"],
                "sentence_den": counts["indichain"]["den"],
                "unresolved": counts["indichain"]["unresolved"],
                "absent": counts["indichain"]["absent"],
                "freeze": percent(counts["indichain"]["freeze_ok"], counts["indichain"]["freeze_den"]),
                "freeze_ok": counts["indichain"]["freeze_ok"],
                "freeze_den": counts["indichain"]["freeze_den"],
            },
            "ordinary": {
                "novel": percent(ord_novel["new"], ord_novel["of"]),
                "novel_new": ord_novel["new"],
                "names": ord_novel["names"],
                "flags": ord_novel["flags"],
                "sentence": percent(counts["ordinary"]["in"], counts["ordinary"]["den"]),
                "sentence_in": counts["ordinary"]["in"],
                "sentence_den": counts["ordinary"]["den"],
                "unresolved": counts["ordinary"]["unresolved"],
                "absent": counts["ordinary"]["absent"],
                "freeze": percent(counts["ordinary"]["freeze_ok"], counts["ordinary"]["freeze_den"]),
                "freeze_ok": counts["ordinary"]["freeze_ok"],
                "freeze_den": counts["ordinary"]["freeze_den"],
                "sentences": len(ord_sent),
            },
        })
        print(
            f"{slug} new {indi_novel['new']}/5 vs {ord_novel['new']}/5 "
            f"sentence {counts['indichain']['in']}/{counts['indichain']['den']} vs "
            f"{counts['ordinary']['in']}/{counts['ordinary']['den']} "
            f"absent {counts['ordinary']['absent']} unresolved {counts['ordinary']['unresolved']}",
            flush=True,
        )
    out = ORD
    out.mkdir(parents=True, exist_ok=True)
    (out / "percent.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_html(out / "percent.html", summary)
    stale = out / "unopened-full-texts.docx"
    if stale.is_file():
        stale.unlink()


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    elif "--overview" in sys.argv:
        write_overview_docx(BASE / "打分api普通模型" / "three-metric-percentages-overview.docx")
    elif "--ord" in sys.argv:
        folder = Path(sys.argv[sys.argv.index("--ord") + 1])
        label = sys.argv[sys.argv.index("--label") + 1] if "--label" in sys.argv else folder.name
        score_folder(folder, label)
    else:
        main()
