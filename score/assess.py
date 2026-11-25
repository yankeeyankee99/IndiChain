"""Apply the indication assessment score.

Code scores disease-term resolvability, indication novelty against a known-set
file, whether a chain joins the named target to the named disease, and
reference resolvability when a DOI or PMID answers. Mechanistic concordance,
absence of direct refutation, and stepwise evidential support stay unread
until a judgment file supplies them. Missing material stays in the denominator
and is marked; it is not dropped and it is not treated as full marks.
"""

from __future__ import annotations

import hashlib
import html
import http.cookiejar
import json
import re
import unicodedata
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

socket.setdefaulttimeout(12)

WEIGHTS = {
    "mechanistic_concordance": 10,
    "disease_term_resolvability": 5,
    "indication_novelty": 25,
    "absence_of_direct_refutation": 5,
    "indication_non_nesting": 5,
    "stepwise_evidential_support": 20,
    "reference_resolvability": 30,
}
READING = {
    "mechanistic_concordance",
    "absence_of_direct_refutation",
    "stepwise_evidential_support",
}
MESH_ID = re.compile(r"D\d{6,9}\b")
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
PMID = re.compile(r"\b(\d{7,8})\b")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def load_known(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {"target": "", "as_of": "", "known": [], "missing": True}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["missing"] = False
    return data


def known_names(known: dict) -> set[str]:
    found = set()
    for row in known.get("known") or []:
        found.add(norm(row.get("name") or ""))
        for alias in row.get("aliases") or []:
            found.add(norm(alias))
        mesh = str(row.get("mesh") or "")
        if mesh:
            found.add(mesh)
    found.discard("")
    return found


def locate_nominate(folder: Path) -> Path:
    if (folder / "top5.md").is_file() or (folder / "top5.json").is_file():
        return folder
    nested = folder / "nominate"
    if (nested / "top5.md").is_file() or (nested / "top5.json").is_file():
        return nested
    return folder


def parse_top5_json(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("diseases") or []
    if not rows:
        want = int(payload.get("want") or 5)
        rows = (payload.get("candidates") or [])[:want]
    found = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        mesh = str(row.get("mesh") or "")
        if name:
            found.append({"name": name, "mesh": mesh})
    return found


def parse_top5(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = re.match(r"\s*\d+\.\s+(.+?)\s*$", line)
        if not match:
            continue
        body = match.group(1)
        mesh = ""
        id_match = re.search(r"\(([D]\d{6,9})\)", body)
        if id_match:
            mesh = id_match.group(1)
            body = body[: id_match.start()].strip()
        body = re.sub(r"\s+\d+/\d+\s*$", "", body).strip()
        if body:
            rows.append({"name": body, "mesh": mesh})
    return rows


def parse_run_table(text: str) -> list[dict]:
    lines = [line for line in text.splitlines() if line.startswith("|")]
    if len(lines) < 3:
        return []
    header = [cell.strip().casefold() for cell in lines[0].strip("|").split("|")]
    name_at = next((i for i, cell in enumerate(header) if "disease" in cell or "indication" in cell), 0)
    rows = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: "):
            continue
        name = cells[name_at] if name_at < len(cells) else ""
        name = re.sub(r"\s+\([D]\d{6,9}\)\s*$", "", name).strip()
        if name:
            rows.append({"name": name, "mesh": "", "rationale": " | ".join(cells)})
    return rows


def nominate_rows(folder: Path) -> tuple[list[dict], list[str]]:
    notes = []
    folder = locate_nominate(folder)
    top5 = folder / "top5.md"
    if top5.is_file():
        rows = parse_top5(top5.read_text(encoding="utf-8"))
        if rows:
            return rows, notes
        notes.append("top5.md has no numbered disease names")
    top5_json = folder / "top5.json"
    if top5_json.is_file():
        rows = parse_top5_json(top5_json)
        if rows:
            notes.append("names taken from top5.json")
            return rows, notes
        notes.append("top5.json has no disease names")
    runs = sorted(folder.glob("run-*/*.md"))
    runs = [path for path in runs if not path.name.endswith(".reasoning.md")]
    if not runs:
        notes.append("no top5.md and no run table")
        return [], notes
    # One run is not the five-run deliverable. Use it only when top5 is absent.
    rows = parse_run_table(runs[0].read_text(encoding="utf-8"))
    notes.append(f"no top5; using the first five disease names from {runs[0].name}")
    return rows[:5], notes


def pad(rows: list[dict]) -> list[dict]:
    padded = list(rows)
    while len(padded) < 5:
        padded.append({"name": "", "mesh": "", "empty": True})
    return padded


def is_known(row: dict, names: set[str]) -> bool:
    if row.get("empty") or not row.get("name"):
        return True
    if row.get("mesh") and row["mesh"] in names:
        return True
    return norm(row["name"]) in names


def score_nominate(folder: Path, known: dict) -> dict:
    rows, notes = nominate_rows(folder)
    if known.get("missing"):
        notes.append("no known-indication set for this target; indication novelty was not scored")
    slots = pad(rows[:5])
    names = known_names(known)
    notes.append("indication novelty is counted over the five top5 names")
    resolvable = []
    novel_flags = []
    known_hits = []
    seen = set()
    for row in slots:
        if row.get("empty"):
            resolvable.append(0)
            novel_flags.append(0)
            continue
        key = row.get("mesh") or norm(row["name"])
        repeat = key in seen
        seen.add(key)
        resolvable.append(0 if repeat or not row.get("mesh") else 1)
        if repeat:
            novel_flags.append(0)
            notes.append(f"{row['name']} repeats an earlier top5 name; novelty is 0")
            continue
        if not row.get("mesh"):
            notes.append(f"{row['name']} has no MeSH id; disease-term resolvability is 0")
        if is_known(row, names):
            novel_flags.append(0)
            known_hits.append(row["name"])
        else:
            novel_flags.append(1)
    if known_hits:
        notes.append("known indications in the top5: " + "; ".join(known_hits))
    q = sum(resolvable) / 5
    if known.get("missing"):
        novelty = None
        novelty_points = None
    else:
        novelty = sum(novel_flags) / 5
        novelty_points = WEIGHTS["indication_novelty"] * novelty
    items = {
        "mechanistic_concordance": unread(WEIGHTS["mechanistic_concordance"]),
        "disease_term_resolvability": scored(WEIGHTS["disease_term_resolvability"] * q, WEIGHTS["disease_term_resolvability"], q),
        "indication_novelty": (
            unread(WEIGHTS["indication_novelty"], "known-indication set is missing")
            if known.get("missing")
            else scored(novelty_points, WEIGHTS["indication_novelty"], novelty)
        ),
        "absence_of_direct_refutation": unread(WEIGHTS["absence_of_direct_refutation"]),
        "indication_non_nesting": unread(WEIGHTS["indication_non_nesting"]),
        "stepwise_evidential_support": skipped_chain(),
        "reference_resolvability": skipped_chain(),
    }
    return {
        "kind": "nominate",
        "source": str(folder),
        "diseases": slots,
        "notes": notes,
        "items": items,
        "score": total(items),
    }


def unread(max_points: int, reason: str = "reading item, not yet judged") -> dict:
    return {"points": None, "max": max_points, "status": "unread", "reason": reason}


def skipped_chain() -> dict:
    return {"points": None, "max": 0, "status": "not_in_this_part", "reason": "nomination output does not include a mechanism chain"}


def scored(points: float, max_points: int, fraction: float) -> dict:
    return {"points": round(points, 2), "max": max_points, "status": "scored", "fraction": fraction}


def total(items: dict) -> dict:
    got = 0.0
    possible = 0
    pending = []
    for name, row in items.items():
        if row.get("status") == "not_in_this_part":
            continue
        possible += row["max"]
        if row.get("points") is None:
            pending.append(name)
        else:
            got += row["points"]
    return {"points": None if pending else round(got, 2), "scored_points": round(got, 2), "possible": possible, "pending": pending}


def chain_axes(folder: Path) -> tuple[list[dict], list[str]]:
    path = folder / "axes.json"
    if not path.is_file():
        return [], ["no axes.json"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    axes = payload.get("axes") if isinstance(payload, dict) else None
    if not isinstance(axes, list):
        return [], ["axes.json has no axes list"]
    return axes, []


def hop_citations(hop: dict) -> list[dict]:
    found = []
    for sentence in hop.get("sentences") or []:
        doi = str(sentence.get("doi") or "")
        doi_match = DOI.search(doi) or DOI.search(str(sentence.get("sentence") or ""))
        pmid = str(sentence.get("pmid") or "")
        if not pmid:
            pmid_match = PMID.search(str(sentence.get("pmid") or ""))
            pmid = pmid_match.group(1) if pmid_match else ""
        found.append({"doi": doi_match.group(0).rstrip(").,;") if doi_match else "", "pmid": pmid})
    return found


class PubMedBlocked(Exception):
    pass


_PUBMED = "https://pubmed.ncbi.nlm.nih.gov/"
_pubmed_opener = None


def _pubmed_browser():
    global _pubmed_opener
    if _pubmed_opener is None:
        jar = http.cookiejar.CookieJar()
        _pubmed_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return _pubmed_opener


def _zero_bits(digest: bytes) -> int:
    total = 0
    for byte in digest:
        if byte == 0:
            total += 8
            continue
        for bit in range(7, -1, -1):
            if byte & (1 << bit):
                return total
            total += 1
    return total


def _pubmed_html(url: str) -> tuple[str, str]:
    time.sleep(0.4)
    opener = _pubmed_browser()
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(request, timeout=30) as response:
        page = response.read().decode("utf-8", "replace")
        final = response.geturl()
    match = re.search(r'const challengeId = "([^"]+)"', page)
    if not match:
        return page, final
    bits = int(re.search(r"const bits = (\d+)", page).group(1))
    nonce = 0
    while _zero_bits(hashlib.sha256(f"{match.group(1)}:{nonce}".encode()).digest()) < bits:
        nonce += 1
    body = json.dumps({"challenge_id": match.group(1), "nonce": str(nonce), "redirect_url": _PUBMED}).encode()
    solved = urllib.request.Request(
        _PUBMED + "_pow/solve",
        data=body,
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
    )
    with opener.open(solved, timeout=30) as response:
        response.read()
    with opener.open(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace"), response.geturl()


def _page_blocked(page: str) -> bool:
    # ponytail: search HTML can contain "No results" before the result list is filled, so that page stays unread rather than a zero. A real miss is decided from a loaded PubMed results page.
    if 'name="citation_title"' in page or 'class="docsum-title"' in page:
        return False
    return "const challengeId" in page or "recaptcha/challengepage" in page or "No results were found" in page


def pubmed_ids(term: str) -> list[str]:
    page, final = _pubmed_html(_PUBMED + "?" + urllib.parse.urlencode({"term": term, "size": "2"}))
    if _page_blocked(page):
        raise PubMedBlocked()
    landed = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?", final)
    if landed:
        return [landed.group(1)]
    ids = re.findall(r'class="docsum-title"[^>]*href="/(\d+)/"', page)
    ids += re.findall(r'href="/(\d+)/"[^>]*class="docsum-title"', page)
    if not ids:
        raise PubMedBlocked()
    return list(dict.fromkeys(ids))


def pubmed_record(pmid: str) -> dict | None:
    page, _final = _pubmed_html(_PUBMED + pmid + "/")
    if _page_blocked(page):
        raise PubMedBlocked()
    title_match = re.search(r'<meta name="citation_title" content="([^"]*)"', page)
    abstract_match = re.search(r'id="eng-abstract">(.*?)</div>', page, re.S)
    title = html.unescape(title_match.group(1)) if title_match else ""
    abstract = ""
    if abstract_match:
        abstract = re.sub(r"<[^>]+>", " ", html.unescape(abstract_match.group(1)))
        abstract = re.sub(r"\s+", " ", abstract).strip()
    if not title and not abstract:
        return None
    return {"pmid": pmid, "title": title, "abstractText": abstract}


def paper_record(citation: dict, cache: dict) -> dict | None:
    if citation.get("pmid"):
        key = f"pmid:{citation['pmid']}"
    elif citation.get("doi"):
        key = f"\"{citation['doi']}\"[aid]"
    elif citation.get("query"):
        key = citation["query"]
    else:
        return None
    if key in cache:
        return cache[key]
    if citation.get("pmid"):
        record = pubmed_record(str(citation["pmid"]))
    else:
        ids = pubmed_ids(key)
        record = pubmed_record(ids[0]) if len(ids) == 1 else None
    cache[key] = record
    return record


def resolves(citation: dict, cache: dict) -> bool:
    return paper_record(citation, cache) is not None


def score_chain(folder: Path, target: str, disease: str, check_live: bool) -> dict:
    axes, notes = chain_axes(folder)
    if not axes:
        items = {
            "mechanistic_concordance": skipped_nominate(),
            "disease_term_resolvability": skipped_nominate(),
            "indication_novelty": skipped_nominate(),
            "absence_of_direct_refutation": skipped_nominate(),
            "indication_non_nesting": skipped_nominate(),
            "stepwise_evidential_support": unread(WEIGHTS["stepwise_evidential_support"], "no path to score"),
            "reference_resolvability": scored(0, WEIGHTS["reference_resolvability"], 0),
        }
        return {"kind": "chain", "source": str(folder), "notes": notes, "items": items, "score": total(items), "e": 0}
    best = None
    for index, axis in enumerate(axes):
        hops = axis.get("hops") or []
        if not hops:
            continue
        starts = norm(hops[0].get("left") or "") == norm(target) or norm(target) in norm(hops[0].get("left") or "")
        ends = norm(hops[-1].get("right") or "") == norm(disease) or norm(disease) in norm(hops[-1].get("right") or "")
        if starts and ends and best is None:
            best = (index, hops)
    if best is None:
        notes.append("no axis connects the specified target to the specified disease")
        e = 0
        hops = []
    else:
        e = 1
        notes.append(f"scored path is complete axis {best[0] + 1}, {len(best[1])} steps")
        hops = best[1]
    real = 0
    cited = 0
    empty = 0
    blocked = False
    cache: dict = {}
    if e:
        try:
            for hop in hops:
                citations = hop_citations(hop)
                if not citations:
                    empty += 1
                    continue
                for citation in citations:
                    cited += 1
                    if not check_live:
                        continue
                    if resolves(citation, cache):
                        real += 1
        except PubMedBlocked:
            blocked = True
        if not check_live:
            notes.append("citations were not checked online; reference resolvability was not scored")
        elif blocked:
            notes.append("PubMed records did not open; reference resolvability was not scored")
    denom = cited + empty
    if e and check_live and not blocked:
        fraction = real / denom if denom else 0
        reference = scored(WEIGHTS["reference_resolvability"] * fraction, WEIGHTS["reference_resolvability"], fraction)
    elif e:
        reason = "PubMed record did not open" if blocked else "not checked online"
        reference = unread(WEIGHTS["reference_resolvability"], reason)
    else:
        reference = scored(0, WEIGHTS["reference_resolvability"], 0)
    support = unread(WEIGHTS["stepwise_evidential_support"], "no path to score" if not e else "reading item, not yet judged")
    if not e:
        support = scored(0, WEIGHTS["stepwise_evidential_support"], 0)
    items = {
        "mechanistic_concordance": skipped_nominate(),
        "disease_term_resolvability": skipped_nominate(),
        "indication_novelty": skipped_nominate(),
        "absence_of_direct_refutation": skipped_nominate(),
        "indication_non_nesting": skipped_nominate(),
        "stepwise_evidential_support": support,
        "reference_resolvability": reference,
    }
    return {"kind": "chain", "source": str(folder), "notes": notes, "items": items, "score": total(items), "e": e}


def skipped_nominate() -> dict:
    return {"points": None, "max": 0, "status": "not_in_this_part", "reason": "mechanism-chain output does not include a nomination"}


def write_result(result: dict, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _self_check() -> None:
    text = "# top5\n\n1. Psoriasis (D011565) 5/5\n2. Asthma (D001249) 4/5\n"
    rows = parse_top5(text)
    assert [row["mesh"] for row in rows] == ["D011565", "D001249"]
    known = {"known": [{"name": "Psoriasis", "aliases": ["银屑病"]}], "missing": False}
    assert is_known(rows[0], known_names(known))
    assert not is_known(rows[1], known_names(known))
    empty = score_nominate(Path("missing-nominate-dir"), {"missing": True, "known": []})
    assert empty["items"]["indication_novelty"]["status"] == "unread"
    assert empty["score"]["points"] is None
    payload = {
        "want": 5,
        "diseases": [],
        "candidates": [
            {"name": "Crohn Disease", "mesh": "D003424"},
            {"name": "Arthritis, Rheumatoid", "mesh": "D001172"},
        ],
    }
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        nest = Path(tmp) / "nominate"
        nest.mkdir()
        (nest / "top5.json").write_text(json.dumps(payload), encoding="utf-8")
        assert locate_nominate(Path(tmp)) == nest
        rows = parse_top5_json(nest / "top5.json")
        assert [row["name"] for row in rows] == ["Crohn Disease", "Arthritis, Rheumatoid"]
        (nest / "top5.md").write_text(
            "# top5\n\n1. Crohn Disease (D003424) 5/5\n"
            "2. Arthritis, Rheumatoid (D001172) 4/5\n"
            "3. Asthma (D001249) 3/5\n",
            encoding="utf-8",
        )
        scored_n = score_nominate(Path(tmp), {"missing": False, "known": [{"name": "Arthritis, Rheumatoid"}]})
        assert scored_n["items"]["indication_novelty"]["fraction"] == 2 / 5
    assert sum(WEIGHTS.values()) == 100
    print("self_check ok")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("nominate", "chain", "self-check"))
    parser.add_argument("folder", nargs="?")
    parser.add_argument("--known")
    parser.add_argument("--target", default="")
    parser.add_argument("--disease", default="")
    parser.add_argument("--out", required=False)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.kind == "self-check":
        _self_check()
        raise SystemExit(0)
    folder = Path(args.folder)
    if args.kind == "nominate":
        result = score_nominate(folder, load_known(Path(args.known) if args.known else None))
    else:
        result = score_chain(folder, args.target, args.disease, args.live)
    dest = Path(args.out) if args.out else folder / "assessment.json"
    write_result(result, dest)
    print(dest)
    print(json.dumps(result["score"], ensure_ascii=False))
