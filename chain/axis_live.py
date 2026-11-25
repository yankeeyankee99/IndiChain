"""One target and one disease. Bohrium fills the two ends. PubMed checks each hop.

A hop is kept when a retrieved sentence contains both names. The final files show
only axes that reach a listed pathology node and then the disease.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from threading import Lock, Semaphore, Thread
from urllib.error import HTTPError

from libraries import LiveClients, Paper
from llm import name_problem
from net_live import (
    MODEL_WORKERS, ask_model, harvest, model_slots, pair_query, pair_query_forms, plain_sentence, split_sentences,
)
from run import MODEL_PRO, _deepseek, _load_dotenv, target_named

SIDE_CAP = 300
HOP_FETCH = 50
BATCH_CHARS = 40_000
END_CAP = 5
MID_CAP = 6
BRANCH_CAP = 2
SENTENCE_CAP = 5
# ponytail: a MeSH descriptor can have dozens of entry terms. Eight writings keep the PubMed OR inside one query.
# Upgrade: page the remaining writings when a hop's first eight miss the sentence.
FORM_CAP = 8
# ponytail: five concurrent PubMed harvests returned 429. Upgrade: raise if retries stay ahead of 429.
AXIS_WORKERS = 4
_pubmed_slots = Semaphore(AXIS_WORKERS)
_branch_slots = Semaphore(MODEL_WORKERS)
_pubmed_cache: dict = {}
_pubmed_cache_lock = Lock()
_CJK = re.compile(r"[\u4e00-\u9fff]")


def merge_terms(*groups: list[str]) -> list[str]:
    out = []
    seen = set()
    for group in groups:
        for name in group:
            text = " ".join(str(name).split())
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
    return out


def present_ids(paper: Paper) -> dict:
    out = {}
    if paper.doi:
        out["doi"] = paper.doi
    pmid = paper.pmid or (paper.paper_id if paper.source == "pubmed" and paper.paper_id.isdigit() else "")
    if pmid:
        out["pmid"] = pmid
    if paper.bohrium_id:
        out["paperId"] = paper.bohrium_id
    return out


def sentence_rows(papers: list[Paper], names: list[str], side: str) -> list[dict]:
    rows = []
    seen = set()
    for paper in papers:
        for sentence in split_sentences(paper.text):
            if _CJK.search(sentence) or not target_named(sentence, names):
                continue
            key = (paper.paper_id, sentence.casefold())
            if key in seen:
                continue
            seen.add(key)
            row = {
                "date": paper.published.isoformat() if paper.published else "",
                "title": paper.title,
                "sentence": sentence,
                "side": side,
            }
            row.update(present_ids(paper))
            rows.append(row)
    return rows


def char_batches(rows: list[dict], limit: int = BATCH_CHARS) -> list[list[dict]]:
    """ponytail: one sentence longer than limit is its own batch. Upgrade: split that sentence."""
    batches = []
    current = []
    size = 0
    for row in rows:
        piece = len(row["sentence"]) + 8
        if current and size + piece > limit:
            batches.append(current)
            current = []
            size = 0
        current.append(row)
        size += piece
    if current:
        batches.append(current)
    return batches


def _tab_line(reply: str, label: str) -> list[list[str]]:
    found = []
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if parts and parts[0].upper() == label:
            found.append(parts)
    return found


def indexed_prompt(rows: list[dict]) -> str:
    return "\n".join(f"{i}\t{row['sentence']}" for i, row in enumerate(rows, 1))


class NotASpan(RuntimeError):
    def __init__(self, line: str):
        self.line = line
        super().__init__(line)


def node_name(text: str) -> str:
    """Drop a trailing parenthetical when the name is stored. A parenthesis in the middle stays."""
    name = " ".join((text or "").split())
    while name.endswith((")", "）")):
        depth = 0
        cut = None
        for index in range(len(name) - 1, -1, -1):
            char = name[index]
            if char in ")）":
                depth += 1
            elif char in "(（":
                depth -= 1
                if depth == 0:
                    cut = index
                    break
        if cut is None:
            break
        name = name[:cut].strip()
    return " ".join(name.split())


def node_forms(text: str) -> list[str]:
    """Display name first. A trailing parenthetical is another writing of the same node."""
    name = " ".join((text or "").split())
    extras: list[str] = []
    while name.endswith((")", "）")):
        depth = 0
        cut = None
        for index in range(len(name) - 1, -1, -1):
            char = name[index]
            if char in ")）":
                depth += 1
            elif char in "(（":
                depth -= 1
                if depth == 0:
                    cut = index
                    break
        if cut is None:
            break
        inner = " ".join(name[cut + 1:-1].split())
        if inner:
            extras.append(inner)
        name = " ".join(name[:cut].split())
    forms = [name] if name else []
    for extra in reversed(extras):
        if extra.casefold() not in {item.casefold() for item in forms}:
            forms.append(extra)
    return forms


def copied_spans(reply: str, rows: list[dict], blocked: list[str], limit: int = END_CAP) -> list[dict]:
    """Keep index, tab, and a node. The program does not check that the node is a span of the sentence."""
    kept = []
    seen = set()
    block = {item.casefold() for item in blocked}
    for line in (reply or "").splitlines():
        raw = line.strip()
        if not raw or raw.upper().startswith("TASK"):
            continue
        index, body = _indexed_span(raw)
        body = " ".join(body.split())
        if not index or not body or body.casefold() == "none":
            continue
        number = int(index)
        if not 1 <= number <= len(rows):
            continue
        if body.casefold() in block or body.casefold() in seen:
            continue
        if name_problem(body, "", ""):
            continue
        seen.add(body.casefold())
        kept.append({
            "name": body,
            "pre": body,
            "forms": node_forms(body),
            "sentence": rows[number - 1],
        })
        if len(kept) == limit:
            break
    return kept


class SelectionStopped(RuntimeError):
    def __init__(self, kind: str, reply: str, picked: list[dict]):
        self.kind = kind
        self.reply = reply
        self.picked = picked
        super().__init__(f"{kind} line is not a retrieved sentence")


def same_target(name: str, blocked: list[str]) -> bool:
    group = {item.casefold() for item in blocked if item}
    return any(form.casefold() in group for form in (node_forms(name) or [name]))


_ACCESSION = re.compile(r"^[OPQ][0-9][A-Z0-9]{3}[0-9](?:-\d+)?$", re.IGNORECASE)


def _indexed_span(text: str) -> tuple[str, str]:
    if "\t" in text:
        left, right = text.split("\t", 1)
        if left.strip().isdigit():
            return left.strip(), " ".join(right.split())
    return "", text


def literature_names(names: list[str]) -> list[str]:
    """Names used to keep a target sentence. An accession is not one of them."""
    return [name for name in names if name and not _ACCESSION.match(name.strip())]


def opening_words(target_names: list[str], disease_terms: list[str]) -> tuple[list[str], list[str]]:
    """Opening searches use these writings. An empty side is not replaced with one original string."""
    left = literature_names(target_names)
    right = []
    seen = set()
    for item in disease_terms:
        text = " ".join(str(item).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            right.append(text)
    if not left or not right:
        raise RuntimeError("opening search has no writings")
    return left, right


def target_class(clients, target: str) -> list[str]:
    """Search writings from the input string and one UniProt record. This list does not delete nodes."""
    names = [target]
    hit = None
    try:
        hit = clients.uniprot(target)
    except Exception:
        hit = None
    # ponytail: a failed lookup blocks only the input name. Upgrade: retry UniProt.
    if hit:
        names.append(str(hit.get("uniprot_id") or ""))
        names.extend(str(item) for item in (hit.get("aliases") or []))
    out = []
    seen = set()
    for name in names:
        text = " ".join(str(name).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


def select_ends(ask, rows: list[dict], *, kind: str, target: str, disease: str, target_names: list[str] | None = None, disease_names: list[str] | None = None) -> list[dict]:
    names = list(target_names or [target])
    blocked = [item for item in [*names, disease, *(disease_names or [])] if item]
    if kind.startswith("target"):
        task = (
            f"Target: {target}\n"
            f"Disease: {disease}\n"
            "These sentences are about this target. They may not name the disease.\n"
            "The nodes written here start a chain whose destination is this disease.\n"
            "Each sentence is separate.\n"
            "For each index, write one or more lines.\n"
            "A line is the index, a tab, and a continuous span of that sentence, or the index, a tab, and none.\n"
            "The span is a downstream process or product that the sentence says this target changes, enables, or is required for.\n"
            "Do not write this disease into a node when the sentence does not state it.\n"
            "Do not copy the target's own current, its location, or a proposed treatment.\n"
            "Do not copy a verb that only states the direction, such as increase, decrease, mediate, or regulate. Keep the process or product.\n"
            "If one sentence names two objects, write each object on its own line.\n"
            "Two molecules are one example.\n"
            "If the sentence states several, write several lines with the same index.\n"
            "If it states none, write the index, a tab, and none.\n"
            "Do not stop before the last index.\n"
        )
    else:
        task = (
            f"Target: {target}\n"
            f"Disease: {disease}\n"
            "These sentences are about this disease. They may not name the drug target.\n"
            "The nodes written here are the disease end that this chain reaches.\n"
            "Each sentence is separate.\n"
            "For each index, write one or more lines.\n"
            "A line is the index, a tab, and a continuous span of that sentence, or the index, a tab, and none.\n"
            "The span is a pathological cell, process, or molecule in that sentence.\n"
            "Do not copy a verb that only states the direction, such as increase or decrease. Keep the cell, process, or molecule.\n"
            "If one sentence names two objects, write each object on its own line.\n"
            "Two molecules are one example.\n"
            "If the sentence states several, write several lines with the same index.\n"
            "If it states none, write the index, a tab, and none.\n"
            "Do not stop before the last index.\n"
        )
    def take_batch(batch: list[dict]) -> list[tuple[dict, str]]:
        print(f"select {kind} chars {sum(len(row['sentence']) for row in batch)}", flush=True)
        reply = ask(
            "TASK ends\n"
            f"Kind: {kind}\n"
            + task
            + "\n"
            + indexed_prompt(batch)
        )
        got = copied_spans(reply, batch, blocked, limit=10**6)
        print(f"select {kind} kept {len(got)}", flush=True)
        return [(item, item["name"]) for item in got]

    batches = char_batches(rows, 8_000)
    if len(batches) > 1:
        with ThreadPoolExecutor(max_workers=min(MODEL_WORKERS, len(batches))) as pool:
            renamed_batches = list(pool.map(take_batch, batches))
    else:
        renamed_batches = [take_batch(batch) for batch in batches]
    picked = []
    for renamed in renamed_batches:
        for item, formal in renamed:
            if not formal or name_problem(formal, "", ""):
                continue
            forms = [formal]
            for extra in node_forms(item.get("pre") or "")[1:]:
                if extra.casefold() not in {form.casefold() for form in forms}:
                    forms.append(extra)
            if formal.casefold() in {row["name"].casefold() for row in picked}:
                continue
            picked.append({**item, "name": formal, "forms": forms})
    return group_objects(ask, picked, kind=kind)


def listed_index(line: str, count: int) -> int | None:
    """1-based index when the line starts with that number. Any text after it is ignored."""
    match = re.match(r"\s*(\d+)\b", line or "")
    if not match or count < 1:
        return None
    number = int(match.group(1))
    if number < 1 or number > count:
        return None
    return number


def listed_name(text: str, terms: list[str]) -> str | None:
    """The listed spelling for a number. none is empty. Any other text is not a name."""
    raw = " ".join((text or "").split())
    if not raw or raw.casefold() == "none":
        return ""
    number = listed_index(raw, len(terms))
    if number is None:
        return None
    return terms[number - 1]


def _object_batches(picked: list[dict], limit: int) -> list[list[dict]]:
    """ponytail: one node longer than limit is its own batch. Upgrade: split that node."""
    batches = []
    current = []
    size = 0
    for row in picked:
        piece = len(row["name"]) + len(row["sentence"]["sentence"]) + 24
        if current and size + piece > limit:
            batches.append(current)
            current = []
            size = 0
        current.append(row)
        size += piece
    if current:
        batches.append(current)
    return batches


def _objects_prompt(picked: list[dict], kind: str, *, sentences: bool) -> str:
    if sentences:
        listed = "\n\n".join(
            f"{i}. Node: {row['name']}\nSentence: {row['sentence']['sentence']}"
            for i, row in enumerate(picked, 1)
        )
        lead = "These lines were copied from sentences. Each line is one extracted node.\n"
    else:
        listed = "\n\n".join(f"{i}. Node: {row['name']}" for i, row in enumerate(picked, 1))
        lead = (
            "These names were merged in separate batches.\n"
            "Join the same object across this list.\n"
            "Do not add an object that is not in this list.\n"
        )
    disease = ""
    if not kind.startswith("target"):
        disease = (
            "On the disease side, a pathological cell can be the whole object.\n"
            "When a line names an event that happens to a molecule, the object is the event.\n"
            "Example: IgA deposition stays IgA deposition, not IgA.\n"
        )
    return (
        "TASK objects\n"
        f"Kind: {kind}\n"
        + lead
        + "A merged node is one object.\n"
        "An object is a process, a molecule, or a cell.\n"
        "A molecule may be an object with no cell and no process.\n"
        "When a process names its cell type, that cell type is part of the object. Another cell type is another object.\n"
        "Species, anatomical site, time, and condition are not part of the object. Leave them out of the merged name.\n"
        "If one extracted line names two objects, that line belongs to both merged nodes.\n"
        "Do not add a cell type, a process, or a molecule that the line does not state.\n"
        "The merged name is the object in ordinary words. It need not be a continuous span of a sentence.\n"
        "Use one merged name for the same object throughout this reply.\n"
        "The following examples show the rules. They are not extra rules.\n"
        "Example:\n"
        "human T cell activation/proliferation belongs to T cell activation and to T cell proliferation.\n"
        "antigen-dependent activation of T-cells during the immune response belongs to T cell activation.\n"
        "T cell proliferation belongs to T cell proliferation.\n"
        "murine splenic T and B cell proliferation belongs to T cell proliferation and to B cell proliferation.\n"
        "A molecule named alone, such as IL-10, stays that molecule.\n"
        + disease
        + "Write one block per merged node.\n"
        "NAME\n"
        "the merged name\n"
        "LINES\n"
        "one number from the list\n"
        "one number from the list\n"
        "A line under LINES is only that number.\n\n"
        + listed
    )


def _leaf_members(src: dict) -> list[dict]:
    found = src.get("members") or []
    if found:
        return [{"name": item["name"], "sentence": item["sentence"]} for item in found]
    return [{"name": src["name"], "sentence": src["sentence"]}]


def _groups_from_reply(reply: str, picked: list[dict]) -> list[dict]:
    """A name the reply omits stays under its own extracted name."""
    grouped: list[dict] = []
    name = ""
    lines: list[str] = []
    mode = ""

    def close() -> None:
        nonlocal name, lines
        members = []
        seen = set()
        for line in lines:
            number = listed_index(line, len(picked))
            if number is None or number in seen:
                continue
            seen.add(number)
            members.extend(_leaf_members(picked[number - 1]))
        if name and members:
            forms = [name]
            for src in (picked[number - 1] for number in seen):
                for form in src.get("forms") or [src["name"]]:
                    if form.casefold() not in {item.casefold() for item in forms}:
                        forms.append(form)
            grouped.append({
                "name": name,
                "pre": name,
                "forms": forms,
                "sentence": members[0]["sentence"],
                "source": "merged",
                "members": members,
            })
        name = ""
        lines = []

    for raw in (reply or "").splitlines():
        text = raw.strip()
        if text == "NAME":
            close()
            mode = "name"
            continue
        if text == "LINES":
            mode = "lines"
            continue
        if not text:
            continue
        if mode == "name" and not name:
            name = " ".join(text.split())
            continue
        if mode == "lines":
            lines.append(" ".join(text.split()))
    close()
    used = {item["name"].casefold() for group in grouped for item in group["members"]}
    for row in picked:
        if any(item["name"].casefold() in used for item in _leaf_members(row)):
            continue
        grouped.append({
            **row,
            "source": row.get("source") or "extracted",
            "members": _leaf_members(row),
        })
    return grouped


def group_objects(ask, picked: list[dict], *, kind: str, batch_chars: int = 8_000) -> list[dict]:
    """Group extracted names into objects. Sentence batches stay small. One later call joins the names."""
    if not picked:
        return []
    batches = _object_batches(picked, batch_chars)
    if len(batches) == 1:
        return _groups_from_reply(ask(_objects_prompt(picked, kind, sentences=True)), picked)

    def one_batch(batch: list[dict]) -> list[dict]:
        return _groups_from_reply(ask(_objects_prompt(batch, kind, sentences=True)), batch)

    with ThreadPoolExecutor(max_workers=min(MODEL_WORKERS, len(batches))) as pool:
        parts = [row for grouped in pool.map(one_batch, batches) for row in grouped]
    # ponytail: the name-only join is one call. Upgrade: split that list if its read times out.
    return _groups_from_reply(ask(_objects_prompt(parts, kind, sentences=False)), parts)


def hop_rows(papers: list[Paper]) -> list[dict]:
    rows = []
    for paper in papers:
        for sentence in split_sentences(paper.text):
            if _CJK.search(sentence):
                continue
            row = {
                "date": paper.published.isoformat() if paper.published else "",
                "title": paper.title,
                "sentence": sentence,
            }
            row.update(present_ids(paper))
            rows.append(row)
    return rows


def _forms(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in (value or []) if str(item).strip()]


def linking_sentences(rows: list[dict], left, right) -> list[dict]:
    """Every sentence that names one writing of each side, in paper order."""
    left_forms = _forms(left)
    right_forms = _forms(right)
    found = []
    seen = set()
    for row in rows:
        sentence = row["sentence"]
        if not (target_named(sentence, left_forms) and target_named(sentence, right_forms)):
            continue
        key = sentence.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(row)
    return found


def choose_evidence(ask, left, right, rows: list[dict]) -> list[dict]:
    """Without a model, keep the first ten in paper order. With a model, label first."""
    if ask is None:
        return (rows or [])[:SENTENCE_CAP]
    return evidence_keep(ask, left, right, rows)


def split_proposals(reply: str, on_path: list[str], blocked_names: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Valid next nodes, then lines that are not a node. The second list is not a literature miss."""
    found = []
    rejected = []
    blocked = {name.casefold() for name in on_path}
    blocked.update(name.casefold() for name in (blocked_names or []) if name)
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if not parts or parts[0].upper().startswith("TASK"):
            continue
        text = parts[-1] if parts[0].upper() == "NODE" and len(parts) > 1 else parts[0]
        text = " ".join(text.lstrip("-").split())
        if not text or text.casefold() in {name.casefold() for name in found} or text.casefold() in {name.casefold() for name in rejected}:
            continue
        if text.casefold() in blocked or name_problem(text, "", "") or same_target(text, blocked_names or []):
            continue
        found.append(text)
        if len(found) == BRANCH_CAP:
            break
    return found, rejected


def proposal_names(reply: str, on_path: list[str], blocked_names: list[str] | None = None) -> list[str]:
    return split_proposals(reply, on_path, blocked_names)[0]


def ask_messages(messages: list[dict]) -> str:
    priority = os.environ.get("NET_PRIORITY") == "1"
    delay = 20 if priority else 90
    for _ in range(12):
        try:
            with model_slots:
                return _deepseek("", model=MODEL_PRO, timeout=900, messages=messages)
        except HTTPError as exc:
            if exc.code == 429:
                print(f"429 pause {delay}s", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 600 if priority else 1800)
                continue
            if exc.code == 402:
                raise
            raise
    raise RuntimeError("model unavailable after rate limits")


def chosen_rows(reply: str, picked: list[dict]) -> list[dict]:
    """Rows whose numbers the reply names. A line that is not a listed number is skipped."""
    kept = []
    seen = set()
    for line in (reply or "").splitlines():
        if not line.strip() or line.strip().upper().startswith("TASK"):
            continue
        number = listed_index(line, len(picked))
        if number is None or number in seen:
            continue
        seen.add(number)
        kept.append(picked[number - 1])
        if len(kept) == END_CAP:
            break
    return kept


def _end_sentence(row: dict) -> str:
    sentence = row.get("sentence")
    if isinstance(sentence, dict):
        return str(sentence.get("sentence") or "")
    return str(sentence or "")


def choose_starts(ask, picked: list[dict], *, target: str, disease: str, side: str = "target") -> list[dict]:
    """At most five merged nodes. Fewer stays fewer. The disease side is not a walk."""
    if len(picked) <= END_CAP:
        return list(picked)
    if side == "disease":
        role = (
            "These merged nodes are on the disease side.\n"
            f"Keep at most {END_CAP} that the sentences name as pathology of this disease.\n"
        )
    else:
        role = (
            "These merged nodes are on the target side.\n"
            f"Keep at most {END_CAP} as the first steps of a mechanism from the target toward the disease.\n"
            "A first step is an object the sentences say this target changes.\n"
            "Do not keep the target itself.\n"
            "Do not keep a node that is only a later feature of the disease.\n"
        )
    reply = ask(
        "TASK starts\n"
        f"Target: {target}\nDisease: {disease}\n"
        "This chain starts at the target and its destination is the disease.\n"
        + role
        + "Fewer is correct. Do not add a line to reach that number.\n"
        "One line is one number from the list. Do not write the name.\n\n"
        + "\n\n".join(
            f"{i}. {row['name']}\nSentence: {_end_sentence(row)}" for i, row in enumerate(picked, 1)
        )
    )
    return chosen_rows(reply, picked)


def side_after_merge(selected: list[dict], *, direct: bool, judge) -> tuple[list[dict], dict]:
    """Merged names go to the five starts. A direct run does not look them up or judge identity."""
    if direct:
        return list(selected), {}
    return judge(selected)


def direct_ends(payload: dict) -> tuple[list[dict], list[dict]]:
    """The saved five on each side. The walk uses these names and does not pick again."""
    def revive(rows) -> list[dict]:
        found = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = " ".join(str(row.get("name") or "").split())
            if not name:
                continue
            sentence = row.get("sentence") or ""
            if isinstance(sentence, dict):
                sentence = sentence.get("sentence") or ""
            found.append({"name": name, "forms": [name], "sentence": {"sentence": str(sentence)}})
        return found

    return revive(payload.get("target")), revive(payload.get("disease"))


def with_filter(query: str, extra: str = "") -> str:
    text = " ".join((extra or "").split())
    if not text:
        return query
    return f"({query}) AND ({text})"


def pubmed_papers(clients, left: str, right: str, as_of: date | None, left_forms: list[str] | None = None, right_forms: list[str] | None = None, extra: str = "") -> list[Paper]:
    """One PubMed query, reused for this client. Concurrent harvests stay at AXIS_WORKERS."""
    query = with_filter(pair_query_forms(left_forms or [left], right_forms or [right]), extra)
    key = (id(clients), query, as_of, HOP_FETCH)
    with _pubmed_cache_lock:
        cached = _pubmed_cache.get(key)
    if cached is not None:
        return cached
    with _pubmed_slots:
        with _pubmed_cache_lock:
            cached = _pubmed_cache.get(key)
        if cached is not None:
            return cached
        try:
            papers = harvest(clients, query, as_of, HOP_FETCH)
        except HTTPError as exc:
            if exc.code == 400:
                raise SystemExit(f"stop: PubMed rejected {query}") from exc
            raise
        with _pubmed_cache_lock:
            _pubmed_cache[key] = papers
        return papers


def form_sentences(clients, left_forms: list[str], right_forms: list[str], as_of: date | None, ask=None, extra: str = "") -> tuple[list[dict], str]:
    """Linking sentences in paper order. Evidence labels fill the ten seats."""
    papers = pubmed_papers(clients, left_forms[0], right_forms[0], as_of, left_forms, right_forms, extra)
    found = linking_sentences(hop_rows(papers), left_forms, right_forms)
    kept = choose_evidence(ask, left_forms, right_forms, found)
    if kept:
        return kept, ""
    if not found:
        return [], "no literature"
    return [], "no evidence"


def concept_forms(written: str, mesh: dict, uniprot: dict) -> dict:
    forms = []
    seen = set()
    for name in [written, *(mesh.get("forms") or []), *(uniprot.get("aliases") or [])]:
        text = " ".join(str(name).split())
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        forms.append(text)
        if len(forms) == FORM_CAP:
            break
    return {
        "mesh": str(mesh.get("mesh") or ""),
        "uniprot": str(uniprot.get("uniprot") or ""),
        "forms": forms or [written],
        "trees": [str(tree) for tree in (mesh.get("trees") or []) if str(tree).strip()],
    }


def lookup_concept(clients, name: str) -> dict:
    written = " ".join(str(name).split())
    mesh = {}
    uniprot = {}
    try:
        mesh = clients.mesh_concept(written) or {}
    except Exception:
        mesh = {}
    if mesh.get("mesh") and hasattr(clients, "mesh_trees"):
        try:
            mesh = {**mesh, "trees": clients.mesh_trees(mesh["mesh"]) or []}
        except Exception:
            mesh = {**mesh, "trees": []}
    try:
        hit = clients.uniprot(written)
        uniprot = {"uniprot": str(hit.get("uniprot_id") or ""), "aliases": list(hit.get("aliases") or [])}
    except Exception:
        uniprot = {}
    return concept_forms(written, mesh, uniprot)


def with_record(end: dict, record: dict) -> dict:
    forms = list(end.get("forms") or [end.get("name") or ""])
    for form in record.get("forms") or []:
        if form.casefold() not in {item.casefold() for item in forms}:
            forms.append(form)
        if len(forms) == FORM_CAP:
            break
    item = dict(end)
    item["forms"] = [form for form in forms if form]
    item["record"] = {"mesh": record.get("mesh") or "", "uniprot": record.get("uniprot") or ""}
    return item


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _library_ids(end: dict) -> tuple[str, str]:
    record = end.get("record") or {}
    return _clean(record.get("mesh")), _clean(record.get("uniprot"))


def _id_conflict(members: list[dict]) -> bool:
    """Both sides already have that id type, and the values differ. An empty id is not a conflict."""
    meshes = []
    uniprots = []
    for end in members:
        mesh_id, uniprot_id = _library_ids(end)
        if mesh_id:
            meshes.append(mesh_id.casefold())
        if uniprot_id:
            uniprots.append(uniprot_id.casefold())
    return len(set(meshes)) > 1 or len(set(uniprots)) > 1


def _combined_record(members: list[dict]) -> dict:
    mesh = ""
    uniprot = ""
    for end in members:
        mesh_id, uniprot_id = _library_ids(end)
        mesh = mesh or mesh_id
        uniprot = uniprot or uniprot_id
    return {"mesh": mesh, "uniprot": uniprot}


def _cap_forms(names: list[str]) -> list[str]:
    forms = []
    seen = set()
    for name in names:
        text = _clean(name)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        forms.append(text)
        if len(forms) >= FORM_CAP:
            break
    return forms


IDENTITY_TASK = """本对靶点：{target}
本对疾病：{disease}
本对疾病的 MeSH：{mesh}
{card}{locked_text}这一侧待判断的名字：
{listed}

每个名字后面是已经取到的库记录，可能为空。
你可以调用 MeSH 和 UniProt。一次回复只写下面三种之一。

MESH 检索式
UNIPROT 检索式
IDENTITY
{{"groups":[{{"names":[1],"display":1,"role":"node"}}]}}

role 只写 target、endpoint 或 node。
target 表示这些名字就是本对的靶点。
endpoint 表示这些名字就是本对的终点疾病。
node 表示留下作为路径节点，同一组里的名字是同一个节点。
两个过程不是同一个节点。两个分子不是同一个节点。一个分子和它参与的过程不是同一个节点。
names 里只写待判断名字的序号。
display 写这一组里一个名字的序号。已经留下的节点用它在名单里的序号作为 display，不要把它的序号写进 names。
检索式整段由你写。程序原样发给对应的库，并把库的返回作为下一条消息给你。
"""


def _identity_command(reply: str) -> tuple[str, str]:
    from intake import _command

    kind, body = _command(reply)
    if kind:
        return kind, body
    lines = [line.strip() for line in (reply or "").splitlines() if line.strip()]
    if not lines:
        return "", ""
    head, _, rest = lines[0].partition(" ")
    if head.upper() != "IDENTITY":
        return "", ""
    parts = []
    if rest.strip() and rest.strip() != "检索式":
        parts.append(rest.strip())
    parts.extend(lines[1:])
    return "IDENTITY", "\n".join(parts).strip()


def _group_records(members: list[dict]) -> list[dict]:
    return [
        {"name": end.get("name") or "", "mesh": _library_ids(end)[0], "uniprot": _library_ids(end)[1]}
        for end in members
    ]


def apply_identity(ends: list[dict], groups, locked: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Apply the model's groups. Same ids do not merge. Empty ids do not block a group."""
    locked_ends = [dict(end) for end in (locked or [])]
    locked_keys = {_clean(end.get("name")).casefold() for end in locked_ends}
    by_name = {}
    order = []
    for end in ends:
        key = _clean(end.get("name")).casefold()
        if not key or key in by_name or key in locked_keys:
            continue
        by_name[key] = end
        order.append(key)
    report = {"applied": [], "unjudged": [], "conflicts": [], "skipped": [], "missing": groups is None}
    if groups is None:
        report["unjudged"] = [by_name[key].get("name") or "" for key in order]
        return locked_ends + [dict(by_name[key]) for key in order], report

    def host_for(display: str) -> dict | None:
        key = _clean(display).casefold()
        if not key:
            return None
        for end in locked_ends:
            writings = [_clean(end.get("name")), *[_clean(item) for item in (end.get("forms") or [])]]
            if key in {item.casefold() for item in writings if item}:
                return end
        return None

    assigned = set()
    removed = set()
    merged = {}
    for group in groups or []:
        if not isinstance(group, dict):
            report["skipped"].append({"names": [], "role": "", "display": "", "reason": "group"})
            continue
        role = _clean(group.get("role")).casefold()
        display = _clean(group.get("display"))
        raw_names = group.get("names") or []
        if isinstance(raw_names, str):
            raw_names = [raw_names]
        members = []
        for raw in raw_names:
            key = _clean(raw).casefold()
            end = by_name.get(key)
            if end is None or key in assigned:
                continue
            members.append(end)
        if role not in {"target", "endpoint", "node"}:
            report["skipped"].append({
                "names": [end.get("name") or "" for end in members],
                "role": role,
                "display": display,
                "reason": "role",
            })
            continue
        if not members:
            continue
        host = host_for(display) if role == "node" else None
        compared = ([host] if host else []) + members
        if len(compared) >= 2 and _id_conflict(compared):
            report["conflicts"].append({
                "names": [end.get("name") or "" for end in members],
                "role": role,
                "display": display,
                "records": _group_records(compared),
            })
            continue
        records = _group_records(members)
        if role in {"target", "endpoint"}:
            for end in members:
                key = _clean(end.get("name")).casefold()
                assigned.add(key)
                removed.add(key)
            report["applied"].append({"names": [end.get("name") or "" for end in members], "display": display, "role": role, "records": records})
            continue
        if host is not None:
            host["forms"] = _cap_forms([
                *(host.get("forms") or [host.get("name") or ""]),
                *[end.get("name") or "" for end in members],
                *[form for end in members for form in (end.get("forms") or [])],
            ])
            host["record"] = _combined_record([host, *members])
            for end in members:
                key = _clean(end.get("name")).casefold()
                assigned.add(key)
                removed.add(key)
            report["applied"].append({
                "names": [host.get("name") or "", *[end.get("name") or "" for end in members]],
                "display": host.get("name") or "",
                "role": "node",
                "records": _group_records([host, *members]),
            })
            continue
        member_keys = {_clean(end.get("name")).casefold() for end in members}
        if display.casefold() not in member_keys:
            report["skipped"].append({
                "names": [end.get("name") or "" for end in members],
                "role": role,
                "display": display,
                "reason": "display",
            })
            continue
        earliest_key = min(member_keys, key=order.index)
        shown = next(end.get("name") or "" for end in members if _clean(end.get("name")).casefold() == display.casefold())
        item = dict(by_name[earliest_key])
        item["name"] = shown
        item["forms"] = _cap_forms([
            *[end.get("name") or "" for end in members],
            *[form for end in members for form in (end.get("forms") or [])],
        ])
        item["record"] = _combined_record(members)
        merged[earliest_key] = item
        for key in member_keys:
            assigned.add(key)
            if key != earliest_key:
                removed.add(key)
        report["applied"].append({"names": [end.get("name") or "" for end in members], "display": shown, "role": "node", "records": records})
    kept = list(locked_ends)
    for key in order:
        if key in removed:
            continue
        if key in merged:
            kept.append(merged[key])
            continue
        if key not in assigned:
            report["unjudged"].append(by_name[key].get("name") or "")
        kept.append(dict(by_name[key]))
    return kept, report


def removed_writings(report: dict) -> list[str]:
    found = []
    seen = set()
    for item in report.get("applied") or []:
        if item.get("role") not in {"target", "endpoint"}:
            continue
        for name in [*(item.get("names") or []), item.get("display") or ""]:
            text = _clean(name)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                found.append(text)
    return found


def _shown_name(display, shown: list[str]) -> str | None:
    if isinstance(display, bool):
        return None
    text = str(display) if isinstance(display, int) else _clean(display)
    number = listed_index(text, len(shown))
    if number is None:
        return None
    return shown[number - 1]


def _resolve_groups(groups, shown: list[str]):
    """Turn name and display numbers into the listed spellings. A non-number is not a member."""
    resolved = []
    for group in groups:
        if not isinstance(group, dict):
            resolved.append(group)
            continue
        item = dict(group)
        raw_names = item.get("names") or []
        if isinstance(raw_names, (str, int)) and not isinstance(raw_names, bool):
            raw_names = [raw_names]
        names = []
        for raw in raw_names:
            found_name = _shown_name(raw, shown)
            if found_name and found_name not in names:
                names.append(found_name)
        item["names"] = names
        found = _shown_name(item.get("display"), shown)
        if found is not None:
            item["display"] = found
        elif _clean(item.get("role")).casefold() == "node":
            item["display"] = ""
        resolved.append(item)
    return resolved


def _numbered_ends(ends: list[dict], start: int) -> tuple[str, list[str]]:
    lines = []
    names = []
    for offset, end in enumerate(ends):
        name = end.get("name") or ""
        names.append(name)
        lines.append(f"{start + offset}. {name}\tmesh {_library_ids(end)[0]}\tuniprot {_library_ids(end)[1]}")
    return "\n".join(lines), names


def judge_identity(
    ask,
    ends: list[dict],
    *,
    target: str,
    disease: str,
    mesh: str,
    mesh_search,
    uniprot_search,
    locked: list[dict] | None = None,
    target_record: dict | None = None,
) -> tuple[list[dict], dict]:
    """One side, one judgment. The model writes the groups. The program does not merge on ids."""
    from intake import ROUNDS

    pending_text, pending_names = _numbered_ends(ends, 1)
    locked_text_body, locked_names = _numbered_ends(locked or [], len(pending_names) + 1)
    shown_names = pending_names + locked_names
    card = ""
    if target_record and (target_record.get("mesh") or target_record.get("uniprot")):
        card = f"入口记录 mesh {_clean(target_record.get('mesh'))} uniprot {_clean(target_record.get('uniprot'))}\n"
    locked_text = ("已经留下的节点，不要重判：\n" + locked_text_body + "\n") if locked_text_body else ""
    messages = [{
        "role": "user",
        "content": IDENTITY_TASK.format(
            target=target,
            disease=disease,
            mesh=mesh or "",
            card=card,
            locked_text=locked_text,
            listed=pending_text or "(none)",
        ),
    }]
    for _ in range(ROUNDS):
        reply = ask(messages)
        messages.append({"role": "assistant", "content": reply})
        kind, body = _identity_command(reply)
        if kind == "MESH":
            result = library_result(mesh_search, body)
        elif kind == "UNIPROT":
            result = library_result(uniprot_search, body)
        elif kind == "IDENTITY":
            try:
                data = json.loads(body)
                groups = data.get("groups") if isinstance(data, dict) else data
                if not isinstance(groups, list):
                    raise ValueError("groups")
                return apply_identity(ends, _resolve_groups(groups, shown_names), locked)
            except (ValueError, json.JSONDecodeError, TypeError):
                break
        else:
            break
        messages.append({"role": "user", "content": json.dumps(result, ensure_ascii=False)})
    return apply_identity(ends, None, locked)


TARGET_RECORD_TASK = """靶点字符串：{target}

chain 需要这个靶点在 MeSH 和 UniProt 里的编号。
你可以调用 MeSH 和 UniProt。一次回复只写下面三种之一。

MESH 检索式
UNIPROT 检索式
RECORD
{{"mesh":"","uniprot":"","names":[]}}

检索式整段由你写。程序原样发给对应的库，并把库的返回作为下一条消息给你。
names 里放你要交给 chain 的写法。
mesh 和 uniprot 只填写这次对话里库返回过的编号。
"""


def _record_command(reply: str) -> tuple[str, str]:
    from intake import _command

    kind, body = _command(reply)
    if kind:
        return kind, body
    lines = [line.strip() for line in (reply or "").splitlines() if line.strip()]
    if not lines:
        return "", ""
    head, _, rest = lines[0].partition(" ")
    if head.upper() != "RECORD":
        return "", ""
    parts = []
    if rest.strip() and rest.strip() != "检索式":
        parts.append(rest.strip())
    parts.extend(lines[1:])
    return "RECORD", "\n".join(parts).strip()


def _returned_ids(result: dict) -> tuple[set[str], set[str]]:
    meshes = {_clean(row.get("mesh")) for row in (result.get("records") or []) if isinstance(row, dict)}
    uniprots = {_clean(row.get("accession")) for row in (result.get("rows") or []) if isinstance(row, dict)}
    meshes.discard("")
    uniprots.discard("")
    return meshes, uniprots


def _library_terms(result: dict, kind: str) -> list[str]:
    """Names a library call actually returned. The query string is not one of them."""
    found = []
    if kind == "MESH":
        for record in result.get("records") or []:
            if isinstance(record, dict):
                found.extend(record.get("terms") or [])
    if kind == "UNIPROT":
        for row in result.get("rows") or []:
            if isinstance(row, dict):
                found.extend([row.get("gene") or "", row.get("recommended") or ""])
    out = []
    seen = set()
    for item in found:
        text = " ".join(str(item).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


def library_result(search, body: str) -> dict:
    """A refused library call is an empty result. The pair keeps going."""
    try:
        result = search(body)
    except Exception:
        return {"error": "unavailable", "query": body}
    return result if isinstance(result, dict) else {}


def ask_target_ids(ask, target: str, mesh_search, uniprot_search) -> dict:
    """The model writes the library queries. An id is kept only when a reply in this conversation contains it."""
    from intake import ROUNDS

    messages = [{"role": "user", "content": TARGET_RECORD_TASK.format(target=target)}]
    seen_mesh: set[str] = set()
    seen_uniprot: set[str] = set()
    queries = []
    returned: list[str] = []
    for _ in range(ROUNDS):
        reply = ask(messages)
        messages.append({"role": "assistant", "content": reply})
        kind, body = _record_command(reply)
        if kind in {"MESH", "UNIPROT"}:
            queries.append({"kind": kind, "query": body})
            result = library_result(mesh_search if kind == "MESH" else uniprot_search, body)
            meshes, uniprots = _returned_ids(result if isinstance(result, dict) else {})
            seen_mesh.update(meshes)
            seen_uniprot.update(uniprots)
            for term in _library_terms(result if isinstance(result, dict) else {}, kind):
                if term.casefold() not in {item.casefold() for item in returned}:
                    returned.append(term)
        elif kind == "RECORD":
            try:
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("RECORD")
                mesh_id = _clean(data.get("mesh"))
                uniprot_id = _clean(data.get("uniprot"))
                raw_names = data.get("names") or []
                if isinstance(raw_names, str):
                    raw_names = [raw_names]
                return {
                    "mesh": mesh_id if mesh_id in seen_mesh else "",
                    "uniprot": uniprot_id if uniprot_id in seen_uniprot else "",
                    "names": [_clean(item) for item in raw_names if _clean(item)],
                    "returned": returned,
                    "queries": queries,
                    "rejected_mesh": mesh_id if mesh_id and mesh_id not in seen_mesh else "",
                    "rejected_uniprot": uniprot_id if uniprot_id and uniprot_id not in seen_uniprot else "",
                    "missing": False,
                }
            except (ValueError, json.JSONDecodeError, TypeError):
                break
        else:
            break
        messages.append({"role": "user", "content": json.dumps(result, ensure_ascii=False)})
    return {
        "mesh": "",
        "uniprot": "",
        "names": [],
        "returned": returned,
        "queries": queries,
        "rejected_mesh": "",
        "rejected_uniprot": "",
        "missing": True,
    }


def _in_shown(name: str, messages: list[dict]) -> bool:
    return any(target_named(message.get("content") or "", [name]) for message in messages)


def _remember(ledger: list[dict], reason: str, example: str) -> None:
    if any(item.get("reason") == reason for item in ledger):
        return
    ledger.append({"reason": reason, "example": example})


def _ledger_text(ledger: list[dict]) -> str:
    if not ledger:
        return ""
    return "\n".join(f"{item['reason']}. Example: {item['example']}" for item in ledger) + "\n"


def _known_writing(name: str, catalog: list[dict], blocked: list[str]) -> bool:
    forms = {form.casefold() for form in (node_forms(name) or [name])}
    known = {_clean(item).casefold() for item in blocked if item}
    for end in catalog:
        known.update(item.casefold() for item in (end.get("forms") or []) if item)
        if end.get("name"):
            known.add(end["name"].casefold())
    return bool(forms & known)


def _listed(name: str, writings: list[str] | None) -> bool:
    key = " ".join(str(name).split()).casefold()
    return bool(key) and key in {" ".join(str(item).split()).casefold() for item in (writings or []) if item}


class WritingGroups:
    """One set of names is one node. Any name in the set returns the whole set."""

    def __init__(self) -> None:
        self._groups: list[list[str]] = []
        self._index: dict[str, int] = {}
        self._lock = Lock()

    def add(self, names: list[str]) -> None:
        cleaned = []
        seen = set()
        for name in names:
            text = " ".join(str(name).split())
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                cleaned.append(text)
        if not cleaned:
            return
        with self._lock:
            ids = {self._index[text.casefold()] for text in cleaned if text.casefold() in self._index}
            if not ids:
                self._groups.append(cleaned)
                gid = len(self._groups) - 1
            else:
                gid = min(ids)
                merged = list(self._groups[gid])
                for other in sorted(ids):
                    if other == gid:
                        continue
                    for text in self._groups[other]:
                        if text.casefold() not in {item.casefold() for item in merged}:
                            merged.append(text)
                    self._groups[other] = []
                for text in cleaned:
                    if text.casefold() not in {item.casefold() for item in merged}:
                        merged.append(text)
                self._groups[gid] = merged
            for text in self._groups[gid]:
                self._index[text.casefold()] = gid

    def known(self, name: str) -> bool:
        return " ".join(str(name).split()).casefold() in self._index

    def members(self, name: str) -> list[str]:
        text = " ".join(str(name).split())
        gid = self._index.get(text.casefold())
        if gid is None:
            return [text] if text else []
        return list(self._groups[gid])


def walk_axes(chat, clients, target: str, target_end: dict, disease_ends: list[dict], disease: str, as_of: date | None, target_names: list[str] | None = None, concept_of=None, ledger: list[dict] | None = None, identity_block: list[str] | None = None, judge_name=None, groups: WritingGroups | None = None, disease_names: list[str] | None = None) -> tuple[list[list[dict]], list[dict]]:
    """One conversation for this axis. A later axis starts a new conversation."""
    if ledger is None:
        ledger = []
    if identity_block is None:
        identity_block = []
    if groups is None:
        groups = WritingGroups()
    catalog = []
    for row in disease_ends:
        forms = row.get("forms") or node_forms(row.get("name") or "")
        if not forms:
            continue
        catalog.append({**row, "name": forms[0], "forms": forms})
    shown = "\n".join(
        str((row or {}).get("sentence") or "")
        for row in (target_end.get("evidence") or [])
        if isinstance(row, dict)
    )
    opening = {
        "role": "user",
        "content": (
            f"Target: {target}\nDisease: {disease}\n"
            f"This axis starts at: {target_end['name']}\n"
            "Pathology nodes already read:\n"
            + "\n".join(row["name"] for row in catalog)
            + ("\nSentences already read:\n" + shown if shown else "")
            + "\nThe task is a mechanism from the target to the disease.\n"
            "When asked, propose each distinct next node, at most 2. One line is one name. Two different objects are two lines.\n"
            + _ledger_text(ledger)
        ),
    }
    start = dict(target_end)
    start_forms = target_end.get("forms") or node_forms(target_end.get("name") or "")
    start["name"] = start_forms[0] if start_forms else target_end.get("name") or ""
    start["forms"] = start_forms or [start["name"]]

    def resolve(name: str) -> tuple[str, list[str]]:
        forms = node_forms(name)
        if not forms:
            return "", []
        for end in catalog:
            known = {item.casefold() for item in end["forms"]}
            if any(form.casefold() in known for form in forms):
                return end["name"], list(end["forms"])
        return forms[0], forms

    def names_of(path: list[dict]) -> list[str]:
        return [step["name"] for step in path]

    def stop_item(path: list[dict], stop: str, proposed: str | None = None) -> dict:
        item = {
            "nodes": names_of(path),
            "steps": [
                {
                    "name": step["name"],
                    "sentences": step.get("sentences") or ([step["sentence"]] if step.get("sentence") else []),
                }
                for step in path
            ],
            "stop": stop,
        }
        if proposed:
            item["proposed"] = proposed
        return item

    def walk(path: list[dict], messages: list[dict]) -> tuple[list[list[dict]], list[dict]]:
        if len(path) - 1 >= MID_CAP:
            return [], [stop_item(path, "six intermediates")]
        left = path[-1]["name"]

        def step_prompt() -> str:
            return (
                f"Left: {left}\nPath so far:\n"
                + "\n".join(names_of(path))
                + "\nPropose each distinct next node that heads toward the pathology nodes.\n"
                "One line is the one display name of one next node.\n"
                "A display name is one object, one process, or one disease.\n"
                "Do not write a path. A line with an arrow is not a name.\n"
                "Do not write a wider class, a narrower subtype, or the same node limited to an organ, tissue, cell, or site that this name does not already contain.\n"
                "At most 2 lines. Two different objects are two lines. Another spelling of the same object is not a second line.\n"
                "Do not repeat a name on this path. Do not name the target.\n"
                + _ledger_text(ledger)
            )

        def read_proposed(base: list[dict]) -> tuple[list[str], list[dict]]:
            user = {"role": "user", "content": step_prompt()}
            reply = chat(base + [user])
            thread = base + [user, {"role": "assistant", "content": reply}]
            proposed = []
            occupied = {step["name"].casefold() for step in path}
            occupied.update(_clean(name).casefold() for name in identity_block if name)
            for line in (reply or "").splitlines():
                parts = [part.strip() for part in line.split("\t") if part.strip()]
                if not parts or parts[0].upper().startswith("TASK"):
                    continue
                text = parts[-1] if parts[0].upper() == "NODE" and len(parts) > 1 else parts[0]
                text = " ".join(text.lstrip("-").split())
                if not text or text.casefold() in {name.casefold() for name in proposed}:
                    continue
                if text.casefold() in occupied or name_problem(text, "", "") or _listed(text, target_names):
                    continue
                missed_pair = f"no literature from {left} to {text}"
                if any(item.get("reason") == missed_pair for item in ledger):
                    continue
                proposed.append(text)
                if len(proposed) == BRANCH_CAP:
                    break
            return proposed, thread

        def arrive(written: str) -> tuple[list[list[dict]], list[dict]]:
            """This disease writing closes the branch. Search, because the previous node is not a selected end."""
            left_forms = path[-1].get("forms") or [left]
            right_forms = _merge_forms(list(disease_names or [disease]), [written, disease])
            sentences, gap = form_sentences(
                clients, left_forms, right_forms, as_of,
                lambda prompt: chat([{"role": "user", "content": prompt}]),
            )
            if gap:
                if gap == "no literature":
                    _remember(ledger, f"no literature from {left} to {disease}", disease)
                return [], [stop_item(path, gap, written)]
            return [path + [{"name": disease, "forms": right_forms, "sentences": sentences}]], []

        def branch(name: str, thread: list[dict]) -> tuple[list[list[dict]], list[dict]]:
            if _listed(name, disease_names):
                return arrive(name)
            if judge_name is not None and not _known_writing(name, catalog, identity_block):
                decision = judge_name(name) or {}
                action = decision.get("action") or "keep"
                if action == "drop":
                    for writing in decision.get("writings") or [name]:
                        text = _clean(writing)
                        if text and text.casefold() not in {_clean(item).casefold() for item in identity_block}:
                            identity_block.append(text)
                    return [], [stop_item(path, "identity", name)]
                if action == "arrive":
                    return arrive(name)
                if action == "join" and decision.get("display"):
                    name = decision["display"]
                    if _listed(name, disease_names):
                        return arrive(name)
            stored, forms = resolve(name)
            if not stored:
                return [], [stop_item(path, "empty name", name)]
            if not groups.known(stored):
                try:
                    groups.add([stored, *forms, *search_writings(chat, stored)])
                except Exception as exc:
                    print(f"writings skip {stored}: {exc}", flush=True)
                    groups.add([stored, *forms])
            else:
                groups.add([stored, *forms])
            forms = groups.members(stored)
            on_path = {step["name"].casefold() for step in path}
            for step in path:
                on_path.update(item.casefold() for item in (step.get("forms") or []) if item)
            if any(item.casefold() in on_path for item in forms):
                return [], []
            left_forms = path[-1].get("forms") or [left]
            sentences, gap = form_sentences(clients, left_forms, forms, as_of, lambda prompt: chat([{"role": "user", "content": prompt}]))
            note = {
                "role": "user",
                "content": f"Kept {left} with {stored}.\n"
                + (
                    "\n".join(row["sentence"] for row in sentences)
                    if sentences
                    else "No literature. This step is not kept."
                ),
            }
            if gap:
                if gap == "no literature":
                    _remember(ledger, f"no literature from {left} to {name}", name)
                    if stored.casefold() != name.casefold():
                        _remember(ledger, f"no literature from {left} to {stored}", stored)
                return [], [stop_item(path, gap, name)]
            nxt = path + [{"name": stored, "forms": forms, "sentences": sentences}]
            if any(stored.casefold() == end["name"].casefold() for end in catalog):
                return [nxt], []
            return walk(nxt, thread + [note])

        def run(proposed: list[str], thread: list[dict]) -> tuple[list[list[dict]], list[dict]]:
            # ponytail: a branch keeps its slot until the subtree returns, so deeper hops run on that thread.
            # Upgrade: a shared queue if one target keeps finding literature at every hop.
            parts: list[tuple[list[list[dict]], list[dict]] | None] = [None] * len(proposed)
            errors: list[BaseException] = []
            workers = []
            inline = []
            if len(proposed) == 1:
                inline.append((0, proposed[0]))
            else:
                for index, name in enumerate(proposed):
                    if not _branch_slots.acquire(blocking=False):
                        inline.append((index, name))
                        continue

                    def go(index: int = index, name: str = name) -> None:
                        try:
                            parts[index] = branch(name, thread)
                        except BaseException as exc:
                            errors.append(exc)
                        finally:
                            _branch_slots.release()

                    worker = Thread(target=go)
                    worker.start()
                    workers.append(worker)
            try:
                for index, name in inline:
                    parts[index] = branch(name, thread)
            finally:
                for worker in workers:
                    worker.join()
            if errors:
                raise errors[0]
            found_here: list[list[dict]] = []
            missed_here: list[dict] = []
            for part in parts:
                if part is None:
                    raise RuntimeError("a branch did not return")
                found_here.extend(part[0])
                missed_here.extend(part[1])
            return found_here, missed_here

        proposed, thread = read_proposed(messages)
        if not proposed:
            return [], [stop_item(path, "no proposal")]
        found_here, missed_here = run(proposed, thread)
        one_miss = (
            len(proposed) == 1
            and not found_here
            and len(missed_here) == 1
            and missed_here[0].get("stop") == "no literature"
            and missed_here[0].get("nodes") == names_of(path)
        )
        if not one_miss:
            return found_here, missed_here
        proposed, thread = read_proposed(thread)
        if not proposed:
            return found_here, missed_here
        again, missed_again = run(proposed, thread)
        return again, missed_here + missed_again

    return walk([start], [opening])


def _question_sides(reply: str) -> dict[str, str]:
    found = {}
    for parts in _tab_line(reply, "QUESTION"):
        if len(parts) < 3:
            continue
        side = parts[1].casefold()
        if side in ("target", "disease") and side not in found:
            found[side] = parts[2]
    return found


def _question_prompt(target: str, disease: str) -> str:
    return (
        "TASK questions\n"
        f"Target: {target}\nDisease: {disease}\n"
        "Write two English questions for a paper search.\n"
        "The target question asks the actions, processes, and products of the target.\n"
        "The disease question asks the pathology of this disease.\n"
        "The target question must not name the disease.\n"
        "QUESTION, tab, target, tab, the question.\n"
        "QUESTION, tab, disease, tab, the question.\n"
    )


def questions(ask, target: str, disease: str, disease_names: list[str] | None = None) -> tuple[str, str]:
    names = [disease, *(disease_names or [])]

    def readable(found: dict[str, str]) -> bool:
        if "target" not in found or "disease" not in found:
            return False
        return not any(name and target_named(found["target"], [name]) for name in names)

    prompt = _question_prompt(target, disease)
    found = _question_sides(ask(prompt))
    if not readable(found):
        raise RuntimeError("questions were not readable")
    return found["target"], found["disease"]


def disease_terms(ask, mesh_names: list[str], disease: str) -> list[str]:
    reply = ask(
        "TASK lexicon\n"
        f"Disease: {disease}\n"
        "Official names:\n" + "\n".join(mesh_names) + "\n"
        "Add abbreviations, aliases, and common names that papers use for this same disease.\n"
        "Include the acronym when readers use one.\n"
        "One line: TERM, tab, the name. No other lines.\n"
    )
    extra = _lexicon_lines(reply)
    print("lexicon reply " + " | ".join((reply or "").splitlines())[:500], flush=True)
    return merge_terms(mesh_names, extra)


def _lexicon_lines(reply: str) -> list[str]:
    extra = []
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if not parts or parts[0].upper() in {"NONE", "TASK"}:
            continue
        extra.append(parts[1] if parts[0].upper() == "TERM" and len(parts) > 1 else parts[0])
    return extra


_WRITING_MARKS = ("NODE", "NAME", "ABBR", "SAME")


def writing_sections(reply: str) -> dict:
    """NODE, NAME, ABBR, and SAME. A marker may share its line with the value."""
    lines = [line.strip() for line in (reply or "").splitlines() if line.strip()]
    sections = {mark: [] for mark in _WRITING_MARKS}
    order = []
    current = ""
    for line in lines:
        parts = line.split(None, 1)
        head = parts[0]
        if head in _WRITING_MARKS:
            current = head
            order.append(head)
            sections[head] = [parts[1]] if len(parts) > 1 else []
            continue
        if not current:
            return {"ok": False, "node": "", "name": "", "abbr": "", "same": []}
        sections[current].append(line)
    if order != list(_WRITING_MARKS):
        return {"ok": False, "node": "", "name": "", "abbr": "", "same": []}
    node, name, abbr, same_lines = (sections[mark] for mark in _WRITING_MARKS)
    if len(node) != 1 or len(name) != 1 or len(abbr) != 1:
        return {"ok": False, "node": "", "name": "", "abbr": "", "same": []}
    same = [] if same_lines == ["none"] else same_lines
    return {"ok": True, "node": node[0], "name": name[0], "abbr": abbr[0], "same": same}


def _kept_writings(parsed: dict, mesh_terms: list[str], *, same: bool) -> list[str]:
    found = []

    def add(text: str) -> None:
        item = " ".join((text or "").split())
        if not item or item.casefold() == "none" or item.casefold() in {kept.casefold() for kept in found}:
            return
        found.append(item)

    resolved = listed_name(parsed.get("name") or "", mesh_terms)
    if resolved:
        add(resolved)
    add(parsed.get("abbr") or "")
    if same:
        for line in parsed.get("same") or []:
            add(line)
    return found


_WRITING_TASK = """Node: {name}
This is the name the chain displays.
Write the other writings used to search for this same node.
You may call MeSH. One reply is only one of the two shapes below.

MESH 检索式
the query, written by you

NODE
the display name
NAME
the number of one common disease name from the numbered list in this conversation, or none
ABBR
one abbreviation papers use for this same node, or none
SAME
one other writing of this same molecule or process on each following line
If there is no such writing, the single line after SAME is none.

NAME is only that number.
SAME is another writing of the same scope, not a copy of every MeSH entry.
"""

_WRITING_DROP = "NAME is not a number from the numbered list in this conversation, so that line is not kept. Write that number, or write NAME as none."


def cached_list(cache: dict, lock: Lock, key: str, fetch) -> list:
    """The first finished list for this key is kept. A second caller reuses it."""
    with lock:
        hit = cache.get(key)
    if hit is not None:
        return list(hit)
    found = list(fetch())
    with lock:
        cache.setdefault(key, found)
        return list(cache[key])


def search_writings(chat, name: str) -> list[str]:
    """Display name plus the writings the model keeps. A disease name must be in this conversation's MeSH return."""
    from intake import _command, live_mesh

    messages = [{"role": "user", "content": _WRITING_TASK.format(name=name)}]
    terms: list[str] = []
    told = False
    # MESH continues the conversation. An unreadable reply stops. Five caps the library calls.
    for _ in range(5):
        reply = chat(messages)
        messages.append({"role": "assistant", "content": reply})
        kind, body = _command(reply)
        if kind == "MESH":
            result = live_mesh(body)
            for record in result.get("records") or []:
                for term in record.get("terms") or []:
                    text = " ".join(str(term).split())
                    if text and text.casefold() not in {item.casefold() for item in terms}:
                        terms.append(text)
            numbered = "\n".join(f"{i}. {term}" for i, term in enumerate(terms, 1))
            note = "NAME is the number of one line below, or none.\n" + numbered
            messages.append({"role": "user", "content": json.dumps(result, ensure_ascii=False) + "\n\n" + note})
            continue
        parsed = writing_sections(reply)
        if not parsed["ok"]:
            return [name]
        kept = _kept_writings(parsed, terms, same=True)
        if listed_name(parsed["name"], terms) is None and not told:
            told = True
            messages.append({"role": "user", "content": _WRITING_DROP})
            continue
        return _merge_forms([name], kept)
    return [name]


def disease_writings(ask, preferred: str, mesh_terms: list[str]) -> list[str]:
    """Given disease name, one synonym from this return, and one abbreviation. Not the entry list."""
    shown = "\n".join(f"{i}. {term}" for i, term in enumerate(mesh_terms, 1)) or preferred
    prompt = (
        f"Disease: {preferred}\n"
        "This is the disease string given for this run. The program keeps it.\n"
        "The numbered lines are MeSH names already returned for this disease.\n"
        f"{shown}\n"
        "NAME is one synonym: the number of one line above, or none. Do not write the name.\n"
        "ABBR is one abbreviation papers use. It may be absent from that list.\n"
        "The program ORs the given name, that synonym, and that abbreviation.\n"
        "That list is the opening search, and it is the list that keeps a sentence.\n"
        "Do not write a wider class, a narrower subtype, or this disease limited to an organ, tissue, cell, or site.\n"
        "NODE\n"
        "one name\n"
        "NAME\n"
        "one number from the list above, or none\n"
        "ABBR\n"
        "one abbreviation, or none\n"
        "SAME\n"
        "none\n"
    )
    reply = ask(prompt)
    parsed = writing_sections(reply)
    if parsed["ok"] and listed_name(parsed["name"], mesh_terms) is None:
        reply = ask(prompt + "\n" + _WRITING_DROP)
        parsed = writing_sections(reply)
    kept = _kept_writings(parsed, mesh_terms, same=False) if parsed["ok"] else []
    return _merge_forms([preferred], kept)


_TARGET_DROP = "NAME is not a number from the list above, so that line is not kept. Write that number, or write NAME as none."


def target_writings(ask, target: str, returned: list[str]) -> list[str]:
    """Given target name, one synonym from this return, and one abbreviation. Not the entry list."""
    shown = "\n".join(f"{i}. {term}" for i, term in enumerate(returned, 1)) or target
    prompt = (
        f"Target: {target}\n"
        "This is the target string given for this run. The program keeps it.\n"
        "The numbered lines are names already returned by UniProt or MeSH.\n"
        f"{shown}\n"
        "NAME is one synonym: the number of one line above, or none. Do not write the name.\n"
        "ABBR is one abbreviation papers use. It may be absent from that list.\n"
        "The program ORs the given name, that synonym, and that abbreviation.\n"
        "That list is the opening search, and it is the list that keeps a sentence.\n"
        "Do not write a wider class, a narrower subtype, or this target limited to an organ, tissue, cell, or site.\n"
        "NODE\n"
        "one name\n"
        "NAME\n"
        "one number from the list above, or none\n"
        "ABBR\n"
        "one abbreviation, or none\n"
        "SAME\n"
        "none\n"
    )
    reply = ask(prompt)
    parsed = writing_sections(reply)
    if parsed["ok"] and listed_name(parsed["name"], returned) is None:
        reply = ask(prompt + "\n" + _TARGET_DROP)
        parsed = writing_sections(reply)
    kept = _kept_writings(parsed, returned, same=False) if parsed["ok"] else []
    return _merge_forms([target], kept)


def target_writings_for_run(path: Path, resume: bool, ask, target: str, returned: list[str]) -> list[str]:
    """A resume reads the saved opening list. A missing file asks once and stores it."""
    if resume and path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, list):
            names = [" ".join(str(item).split()) for item in saved if str(item).strip()]
            if names:
                return names
    names = target_writings(ask, target, returned)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
    return names


def _merge_forms(base: list[str], extra: list[str]) -> list[str]:
    found = []
    for item in [*base, *extra]:
        text = " ".join(str(item).split())
        if text and text.casefold() not in {kept.casefold() for kept in found}:
            found.append(text)
    return found


def outer_sentences(clients, anchor: str, node: str, original: dict | None, as_of: date | None, left_forms: list[str] | None = None, right_forms: list[str] | None = None, ask=None) -> list[dict]:
    """Selection sentence first, then PubMed sentences that name both the anchor and the written node."""
    kept = []
    seen = set()

    def add(row: dict | None) -> None:
        sentence = (row or {}).get("sentence") or ""
        key = sentence.casefold()
        if not sentence or key in seen or len(kept) >= SENTENCE_CAP:
            return
        seen.add(key)
        kept.append(row)

    add(original)
    if not anchor or not node:
        return kept
    try:
        papers = pubmed_papers(clients, anchor, node, as_of, left_forms, right_forms)
    except SystemExit as exc:
        print(f"outer skip {anchor} / {node}: {exc}", flush=True)
        return kept
    found = linking_sentences(hop_rows(papers), left_forms or [anchor], right_forms or [node])
    for row in choose_evidence(ask, left_forms or [anchor], right_forms or [node], found):
        add(row)
    return kept


def assemble_axis(target: str, disease: str, path: list[dict], disease_ends: list[dict], target_forms: list[str] | None = None) -> dict:
    """A walked axis stops at the pathology node. The disease link is attached once per node."""
    hops = [{
        "left": target,
        "right": path[0]["name"],
        "left_forms": list(target_forms or [target]),
        "right_forms": path[0].get("forms") or [path[0]["name"]],
        "sentences": path[0].get("evidence") or [path[0]["sentence"]],
    }]
    for index in range(len(path) - 1):
        hops.append({
            "left": path[index]["name"],
            "right": path[index + 1]["name"],
            "left_forms": path[index].get("forms") or [path[index]["name"]],
            "right_forms": path[index + 1].get("forms") or [path[index + 1]["name"]],
            "sentences": path[index + 1]["sentences"],
        })
    return {"hops": hops}


def disease_joins(axes: list[dict], disease: str, disease_ends: list[dict], disease_names: list[str] | None = None) -> list[dict]:
    """One link from each reached pathology node to the disease, using that node's sentences once."""
    right_forms = list(disease_names or [disease])
    by_name = {end["name"].casefold(): end for end in disease_ends}
    joins = []
    seen = set()
    for axis in axes:
        hops = axis.get("hops") or []
        if not hops:
            continue
        name = str(hops[-1].get("right") or "")
        key = name.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        end = by_name.get(key)
        if end is None:
            continue
        sentence = end.get("sentence")
        joins.append({
            "left": end["name"],
            "right": disease,
            "left_forms": end.get("forms") or [end["name"]],
            "right_forms": right_forms,
            "sentences": end.get("evidence") or ([sentence] if sentence else []),
        })
    return joins


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _source_html(row: dict) -> str:
    bits = []
    doi = str(row.get("doi") or "").strip()
    pmid = str(row.get("pmid") or "").strip()
    paper = str(row.get("paperId") or "").strip()
    if doi:
        bits.append(f'<a href="https://doi.org/{_esc(doi)}">{_esc(doi)}</a>')
    if pmid:
        bits.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{_esc(pmid)}/">{_esc(pmid)}</a>')
    if paper:
        bits.append(_esc(paper))
    return ", ".join(bits)


RELATION_REPLY = (
    "The whole reply is one line, and that line is exactly one of these words, with no punctuation and no explanation:\n"
    "related\n"
    "denied\n"
    "co-occurrence\n"
    "other\n"
)


def _prompt_writings(value) -> list[str]:
    """One string or a list. The label question uses every writing from the search."""
    if isinstance(value, str):
        text = " ".join(value.split())
        return [text] if text else []
    return _merge_forms([], [str(item) for item in (value or [])])


def relation_prompt(left, right, sentence: str) -> str:
    """Ask whether the sentence links these two ends. Direction is one label."""
    return (
        "Left writings, and any one of them is the left end:\n"
        + "\n".join(_prompt_writings(left))
        + "\nRight writings, and any one of them is the right end:\n"
        + "\n".join(_prompt_writings(right))
        + f"\nSentence: {sentence}\n\n"
        "related: the sentence states a link between these two ends. "
        "Raising, lowering, producing, defining, measuring, and treating are the same label. "
        "A negative finding that still states an effect, such as inhibits, reduces, does not increase, or does not treat, is related. "
        "A defining lesion is related even when the sentence also names another disease. "
        "Do not separate direction.\n"
        "denied: the sentence states that one end does not cause the other, "
        "or that the two ends are unrelated, independent, or have no association.\n"
        "co-occurrence: the two ends are only listed side by side. "
        "associated with, correlated with, and in patients with are co-occurrence "
        "when the sentence does not state a feature, cause, treatment, measurement, or change.\n"
        "other: a hope, a hypothesis, or a sentence that does not state a link between these two ends.\n"
        + RELATION_REPLY
    )


EVIDENCE_RELATIONS = {"related", "denied", "co-occurrence"}
RELATION_LABELS = EVIDENCE_RELATIONS | {"other"}


def relation_word(reply: str) -> str:
    """The whole reply must be one of the five words. Any other reply has no label."""
    word = (reply or "").strip().strip(".,:;").casefold()
    if word in RELATION_LABELS:
        return word
    return ""


RELATION_ORDER = ("related", "denied", "co-occurrence")


def relation_counts(rows: list[dict]) -> dict[str, int]:
    """Count sentence labels. A hop is not reduced to one of them."""
    counts = {name: 0 for name in RELATION_ORDER}
    for row in rows or []:
        word = str(row.get("relation") or "")
        if word in counts:
            counts[word] += 1
    return counts


def _count_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {counts[name]}" for name in RELATION_ORDER)


def status_prompt(axes: list[dict], pair: list[dict], trials: list[dict] | None = None, joins: list[dict] | None = None) -> str:
    """One note per hop. Pair sentences are the target–disease search, not a hop."""
    hops = []
    seen = set()
    for axis in axes:
        for hop in axis.get("hops") or []:
            key = (str(hop.get("left") or ""), str(hop.get("right") or ""))
            if key in seen:
                continue
            seen.add(key)
            rows = _evidence_rows(hop.get("sentences") or [])
            hops.append({
                "left": hop.get("left"),
                "right": hop.get("right"),
                "counts": relation_counts(rows),
                "sentences": [
                    {"sentence": row.get("sentence"), "relation": row.get("relation") or ""}
                    for row in rows
                    if str(row.get("sentence") or "").strip()
                ],
            })
    for hop in joins or []:
        key = (str(hop.get("left") or ""), str(hop.get("right") or ""))
        if key in seen:
            continue
        seen.add(key)
        rows = _evidence_rows(hop.get("sentences") or [])
        hops.append({
            "left": hop.get("left"),
            "right": hop.get("right"),
            "counts": relation_counts(rows),
            "sentences": [
                {"sentence": row.get("sentence"), "relation": row.get("relation") or ""}
                for row in rows
                if str(row.get("sentence") or "").strip()
            ],
        })
    body = {
        "axes": len(axes),
        "hops": hops,
        "pair": [
            {"sentence": row.get("sentence"), "relation": row.get("relation") or ""}
            for row in _evidence_rows(pair)[:SENTENCE_CAP]
        ],
        "trials": [
            {"sentence": row.get("sentence"), "relation": row.get("relation") or ""}
            for row in _evidence_rows(trials or [])[:SENTENCE_CAP]
        ],
    }
    return (
        "TASK status\n"
        "You are reading the finished axes, the sentences, and the program labels. "
        "Each sentence keeps its own label. Do not collapse a hop into one label. "
        "related means the sentence states a link. It does not record direction. "
        "co-occurrence means the two ends are only listed side by side. "
        "denied means the sentence states that one end does not cause the other, or that the two ends are unrelated. "
        "Do not treat related as proof that the route holds. "
        "Write a short summary of the evidence as a whole. "
        "Quote a denied sentence when one is present. "
        "Name the hops that are only co-occurrence or denied. "
        "When a sentence is about another tissue or another disease, say so. "
        "Do not retell every hop. "
        "The labels are an aid. Judge the sentences yourself.\n\n"
        + json.dumps(body, ensure_ascii=False)
    )


def _shown_sentence(row: dict) -> bool:
    """Evidence labels are shown. other, and a label that was asked for and missed, are not."""
    if "relation" not in row:
        return True
    return str(row.get("relation") or "") in EVIDENCE_RELATIONS


def _evidence_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows or [] if str(row.get("relation") or "") in EVIDENCE_RELATIONS]


def read_relation(ask, left, right, text: str) -> tuple[str, str]:
    """The reply is one of the five words, or it stays unlabeled."""
    reply = ask(relation_prompt(left, right, text))
    word = relation_word(reply)
    if word:
        return word, ""
    return "", reply


def evidence_keep(ask, left, right, rows: list[dict], limit: int = SENTENCE_CAP) -> list[dict]:
    """Label in list order. other and a failed reply do not take a seat. Stop at the limit."""
    kept = []
    seen = set()
    for row in rows or []:
        if len(kept) >= limit:
            break
        text = str((row or {}).get("sentence") or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        existing = str(row.get("relation") or "")
        if existing in EVIDENCE_RELATIONS:
            kept.append(row)
            continue
        if existing == "other":
            continue
        word, raw = read_relation(ask, left, right, text)
        stored = dict(row)
        stored["relation"] = word
        if raw:
            stored["relation_reply"] = raw
        if word in EVIDENCE_RELATIONS:
            kept.append(stored)
    return kept


def axes_with_evidence(axes: list[dict]) -> tuple[list[dict], list[dict]]:
    """A hop with no evidence label is not kept, and that axis is not shown as complete."""
    kept = []
    dropped = []
    for axis in axes:
        hops = axis.get("hops") or []
        if hops and all(_evidence_rows(hop.get("sentences")) for hop in hops):
            kept.append(axis)
            continue
        nodes = []
        if hops:
            nodes.append(str(hops[0].get("left") or ""))
            nodes.extend(str(hop.get("right") or "") for hop in hops)
        dropped.append({"nodes": [node for node in nodes if node], "stop": "no evidence"})
    return kept, dropped


def _hop_writings(hop: dict, side: str) -> list[str]:
    """Writings saved with this hop. A hop without them uses its display name."""
    stored = hop.get(f"{side}_forms")
    if stored:
        return _prompt_writings(stored)
    return _prompt_writings(hop.get(side) or "")


def label_axes(axes: list[dict], ask) -> None:
    """Each sentence keeps its own label. A hop is not given one label."""
    jobs = []
    seen: dict[tuple, tuple[str, str]] = {}
    for axis in axes:
        for hop in axis.get("hops") or []:
            for row in hop.get("sentences") or []:
                text = str(row.get("sentence") or "").strip()
                if not text or str(row.get("relation") or "") in RELATION_LABELS:
                    continue
                left_forms = _hop_writings(hop, "left")
                right_forms = _hop_writings(hop, "right")
                key = (
                    tuple(item.casefold() for item in left_forms),
                    tuple(item.casefold() for item in right_forms),
                    text.casefold(),
                )
                if key not in seen:
                    seen[key] = ("", "")
                    jobs.append((
                        key, left_forms, right_forms, text,
                        str(hop.get("left") or ""), str(hop.get("right") or ""),
                    ))

    def one(item: tuple) -> tuple:
        key, left_forms, right_forms, text, left_name, right_name = item
        word, reply = read_relation(ask, left_forms, right_forms, text)
        print(f"relation {left_name} → {right_name}: {word or 'unlabeled'}", flush=True)
        return key, word, reply

    if jobs:
        workers = min(MODEL_WORKERS, len(jobs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, word, reply in pool.map(one, jobs):
                seen[key] = (word, reply)
    for axis in axes:
        for hop in axis.get("hops") or []:
            for row in hop.get("sentences") or []:
                text = str(row.get("sentence") or "").strip()
                if not text or str(row.get("relation") or "") in RELATION_LABELS:
                    continue
                key = (
                    tuple(item.casefold() for item in _hop_writings(hop, "left")),
                    tuple(item.casefold() for item in _hop_writings(hop, "right")),
                    text.casefold(),
                )
                word, reply = seen[key]
                row["relation"] = word
                if reply:
                    row["relation_reply"] = reply


def _display_hops(axis: dict, joins: list[dict] | None) -> list[dict]:
    """The disease continues the axis that reached that pathology node. A node is not joined to itself."""
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


def _uniform_kind(rows: list[dict]) -> str:
    """One edge color only when every shown sentence carries that same label."""
    words = []
    for row in rows or []:
        if not _shown_sentence(row):
            continue
        word = str(row.get("relation") or "")
        if word in RELATION_ORDER:
            words.append(word)
    if words and len(set(words)) == 1:
        return words[0]
    return ""


def report_md(target: str, disease: str, axes: list[dict], note: str, incomplete: list[dict] | None = None, joins: list[dict] | None = None) -> str:
    lines = [f"# {target} → {disease}", ""]
    if note:
        lines.extend(["## Summary", "", note, ""])
    for index, axis in enumerate(axes, 1):
        hops = _display_hops(axis, joins)
        if not hops:
            continue
        names = " → ".join([hops[0]["left"], *[hop["right"] for hop in hops]])
        lines.append(f"## {index}. {names}")
        lines.append("")
        for hop in hops:
            lines.append(f"### {hop['left']} → {hop['right']}")
            lines.append("")
            lines.append(_count_text(relation_counts(hop["sentences"])))
            lines.append("")
            for row in hop["sentences"]:
                if not _shown_sentence(row):
                    continue
                word = str(row.get("relation") or "")
                lines.append(f"- [{word}] {row.get('sentence') or ''}")
            lines.append("")
    if incomplete:
        lines.extend(["## Incomplete", ""])
        for item in incomplete:
            route = " → ".join(item["nodes"])
            proposed = item.get("proposed")
            if proposed:
                route = f"{route} → {proposed}"
            lines.append(f"- {route}: {item['stop']}")
        lines.append("")
    return "\n".join(lines)


def _node_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _esc(text))


def note_html(note: str) -> str:
    blocks: list[str] = []
    items: list[tuple[int, str]] = []

    def flush() -> None:
        if not items:
            return
        lis = "".join(
            f'<li style="margin-left:{indent // 2}em">{body}</li>' for indent, body in items
        )
        blocks.append(f"<ul>{lis}</ul>")
        items.clear()

    for raw in (note or "").replace("\t", "  ").splitlines():
        if not raw.strip():
            flush()
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if len(line) >= 3 and set(line) <= {"-", "*"}:
            flush()
            blocks.append("<hr>")
            continue
        if line.startswith("#"):
            flush()
            level = "h4" if line.startswith("###") else "h3"
            blocks.append(f"<{level}>{_inline(line.lstrip('#').strip())}</{level}>")
            continue
        if line.startswith(("- ", "* ")):
            items.append((indent, _inline(line[2:])))
            continue
        flush()
        blocks.append(f"<p>{_inline(line)}</p>")
    flush()
    return "".join(blocks)


def overview_html(target: str, disease: str, axes: list[dict], joins: list[dict] | None = None) -> str:
    label: dict[str, str] = {}
    depth: dict[str, int] = {}
    edges: list[tuple[str, str]] = []
    evidence: dict[tuple[str, str], list] = {}
    seen: set[tuple[str, str]] = set()
    order: list[str] = []
    for axis in axes:
        names = [axis["hops"][0]["left"], *[hop["right"] for hop in axis["hops"]]]
        keys = []
        for index, name in enumerate(names):
            key = _node_key(name)
            if key not in label:
                label[key] = " ".join(name.split())
                depth[key] = index
                order.append(key)
            else:
                depth[key] = max(depth[key], index)
            keys.append(key)
        for hop, left, right in zip(axis["hops"], keys, keys[1:]):
            evidence.setdefault((left, right), []).extend(hop.get("sentences") or [])
            if left != right and (left, right) not in seen:
                seen.add((left, right))
                edges.append((left, right))
    for hop in joins or []:
        left_name = str(hop.get("left") or "")
        right_name = str(hop.get("right") or "")
        left = _node_key(left_name)
        right = _node_key(right_name)
        if left not in label:
            label[left] = " ".join(left_name.split())
            depth[left] = 0
            order.append(left)
        if right not in label:
            label[right] = " ".join(right_name.split())
            depth[right] = depth.get(left, 0) + 1
            order.append(right)
        evidence.setdefault((left, right), []).extend(hop.get("sentences") or [])
        if left != right and (left, right) not in seen:
            seen.add((left, right))
            edges.append((left, right))
    # ponytail: a cycle stops after one pass per node and can share a column. Wires bend a backward edge.
    for _ in range(len(label) + 1):
        moved = False
        for left, right in edges:
            if depth[right] <= depth[left]:
                depth[right] = depth[left] + 1
                moved = True
        if not moved:
            break
    columns: dict[int, list[str]] = {}
    for key in order:
        columns.setdefault(depth[key], []).append(key)
    ids = {key: f"n{i}" for i, key in enumerate(order)}
    target_key = _node_key(target)
    disease_key = _node_key(disease)
    lanes = []
    for col in sorted(columns):
        cells = []
        for key in columns[col]:
            role = " target" if key == target_key else " disease" if key == disease_key else ""
            cells.append(f'<div class="node{role}" data-id="{ids[key]}">{_esc(label[key])}</div>')
        lanes.append(f'<div class="lane">{"".join(cells)}</div>')
    payload = _esc(json.dumps([
        [ids[left], ids[right], _uniform_kind(evidence.get((left, right), []))]
        for left, right in edges
    ]))
    return (
        '<section class="overview"><h2>Overview</h2>'
        f'<div class="fork" data-edges="{payload}">{"".join(lanes)}</div></section>'
    )


_FORK_SCRIPT = """
function drawForks() {
  document.querySelectorAll(".fork").forEach((fork, index) => {
    const svg = fork.querySelector("svg.wires") || document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "wires");
    if (!svg.parentNode) fork.prepend(svg);
    const nodes = {};
    fork.querySelectorAll(".node").forEach((node) => {
      const box = node.getBoundingClientRect();
      const root = fork.getBoundingClientRect();
      nodes[node.dataset.id] = {
        left: box.left - root.left + fork.scrollLeft,
        right: box.right - root.left + fork.scrollLeft,
        cy: box.top - root.top + fork.scrollTop + box.height / 2
      };
    });
    const width = Math.max(fork.scrollWidth, 1);
    const height = Math.max(fork.scrollHeight, 1);
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    const color = getComputedStyle(document.body).getPropertyValue("--deep").trim() || "#3f5c49";
    const edges = JSON.parse(fork.dataset.edges || "[]");
    svg.replaceChildren();
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
    marker.setAttribute("id", "arrow-" + index);
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "8");
    marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    const head = document.createElementNS("http://www.w3.org/2000/svg", "path");
    head.setAttribute("d", "M 0 1.2 L 9 5 L 0 8.8 Z");
    head.setAttribute("fill", color);
    marker.append(head);
    defs.append(marker);
    svg.append(defs);
    const colors = {related: "#0d6b3a", denied: "#a12622", "co-occurrence": "#8a5a00"};
    edges.forEach((edge) => {
      const from = edge[0];
      const to = edge[1];
      const kind = edge[2] || "";
      const a = nodes[from];
      const b = nodes[to];
      if (!a || !b) return;
      const forward = b.left >= a.right - 8;
      const x1 = forward ? a.right : a.left;
      const x2 = forward ? b.left : b.right;
      const bend = forward ? 0 : 28;
      const mid = (x1 + x2) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "M " + x1 + " " + a.cy + " C " + mid + " " + (a.cy + bend) + ", " + mid + " " + (b.cy + bend) + ", " + x2 + " " + b.cy);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", colors[kind] || color);
      path.setAttribute("stroke-width", "1.4");
      if (kind === "co-occurrence") path.setAttribute("stroke-dasharray", "4 3");
      path.setAttribute("marker-end", "url(#arrow-" + index + ")");
      svg.append(path);
    });
  });
}
drawForks();
window.addEventListener("resize", () => requestAnimationFrame(drawForks));
document.querySelectorAll(".fork").forEach((fork) => {
  fork.addEventListener("scroll", () => requestAnimationFrame(drawForks), { passive: true });
});
"""


def _count_html(counts: dict[str, int]) -> str:
    parts = [
        f'<span class="rel {name}">{_esc(name)} {counts[name]}</span>'
        for name in RELATION_ORDER
    ]
    return f'<p class="counts">{"".join(parts)}</p>'


def _sentence_html(row: dict) -> str:
    word = str(row.get("relation") or "")
    kind = word if word in RELATION_ORDER else "unlabeled"
    mark = _esc(word or "unlabeled")
    return (
        f'<div class="card {kind}">'
        f'<div class="meta">{_esc(row.get("title") or "")} {_source_html(row)} '
        f'<span class="rel {kind}">{mark}</span></div>'
        f'<p class="sent {kind}">{_esc(row.get("sentence") or "")}</p></div>'
    )


def _chain_fork(hops: list[dict]) -> str:
    names = [hops[0]["left"], *[hop["right"] for hop in hops]]
    lanes = "".join(
        f'<div class="lane"><div class="node" data-id="n{index}">{_esc(name)}</div></div>'
        for index, name in enumerate(names)
    )
    edges = [
        [f"n{index}", f"n{index + 1}", _uniform_kind(hop.get("sentences"))]
        for index, hop in enumerate(hops)
    ]
    return f'<div class="fork" data-edges="{_esc(json.dumps(edges))}">{lanes}</div>'


def present_html(target: str, disease: str, axes: list[dict], note: str, joins: list[dict] | None = None) -> str:
    blocks = []
    for index, axis in enumerate(axes, 1):
        hops = _display_hops(axis, joins)
        if not hops:
            continue
        sections = []
        for hop in hops:
            cards = "".join(_sentence_html(row) for row in hop["sentences"] if _shown_sentence(row))
            sections.append(
                f'<section><h3>{_esc(hop["left"])} → {_esc(hop["right"])}</h3>'
                f'{_count_html(relation_counts(hop["sentences"]))}{cards}</section>'
            )
        blocks.append(f'<article><h2>Axis {index}</h2>{_chain_fork(hops)}{"".join(sections)}</article>')
    summary = (
        f'<section class="summary"><h2>Summary</h2><div class="note">{note_html(note)}</div></section>'
        if note else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_esc(target)} → {_esc(disease)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 1320px; margin: 0 auto; padding: 28px; color: #1b1b1b; background: #fff; color-scheme: light; --deep: #3f5c49; --line: #b7c3b9; --card-on: #f4f7fb; }}
a {{ color: inherit; }}
.fork {{ position: relative; display: flex; gap: 2.6rem; align-items: stretch; margin: .7rem 0 1.5rem; padding: .35rem .15rem 1.1rem; overflow-x: auto; }}
.fork .lane {{ position: relative; z-index: 1; display: flex; flex: 0 0 10.5rem; flex-direction: column; justify-content: center; gap: .65rem; }}
.fork .node {{ padding: .4rem .5rem; border: 1px solid var(--line); border-radius: .6rem; background: var(--card-on); font-size: .78rem; line-height: 1.3; text-align: center; }}
.fork .node.target {{ border-color: var(--deep); font-weight: 700; }}
.fork .node.disease {{ border-color: var(--deep); color: #f7f3ea; background: var(--deep); font-weight: 700; }}
.fork svg.wires {{ position: absolute; top: 0; left: 0; z-index: 0; overflow: visible; pointer-events: none; }}
.card {{ border: 1px solid #dde2e5; border-left-width: 4px; border-radius: 6px; padding: 12px; margin: 8px 0; }}
.meta {{ color: #1a5276; font-size: .9em; }}
.counts {{ margin: .2rem 0 .6rem; }}
.rel {{ margin-right: .8rem; font-size: .9rem; font-weight: 700; }}
.rel.related, .card.related {{ color: #0d6b3a; }}
.card.related {{ border-color: #0d6b3a; background: #eef7f1; }}
.rel.denied, .card.denied {{ color: #a12622; }}
.card.denied {{ border-color: #a12622; background: #fbeeed; }}
.rel.co-occurrence, .card.co-occurrence {{ color: #8a5a00; }}
.card.co-occurrence {{ border-color: #8a5a00; background: #fbf6ea; }}
.card .meta {{ color: inherit; }}
.note {{ background: #eaf2f8; border-left: 4px solid #1a5276; padding: 12px 16px; }}
.note h3 {{ margin: 1rem 0 .35rem; font-size: 1.05rem; }}
.note h4 {{ margin: .85rem 0 .25rem; font-size: .95rem; }}
.note p {{ margin: .35rem 0; }}
.note ul {{ margin: .2rem 0 .7rem; padding-left: 1.2rem; }}
.note li {{ margin: .28rem 0; }}
</style></head><body>
<h1>{_esc(target)} → {_esc(disease)}</h1>
{summary}
{overview_html(target, disease, axes, joins)}
{''.join(blocks)}
<script>{_FORK_SCRIPT}</script>
</body></html>
"""


def recut_rows(rows: list[dict]) -> list[dict]:
    """Apply the sentence layout rule to rows already stored."""
    out = []
    seen = set()
    for row in rows:
        text = plain_sentence(str(row.get("sentence") or ""))
        if not text:
            continue
        key = (
            row.get("side"),
            row.get("doi") or "",
            row.get("pmid") or "",
            row.get("paperId") or "",
            text.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        if row.get("sentence") != text:
            row = dict(row)
            row["sentence"] = text
        out.append(row)
    return out


def saved_target_ends(partial: dict) -> list[dict] | None:
    """Target ends already chosen when disease selection stopped."""
    found = partial.get("target") or []
    if not str(partial.get("kind") or "").startswith("disease") or not found:
        return None
    ends = []
    for end in found:
        sentence = dict(end.get("sentence") or {})
        if sentence.get("sentence"):
            sentence["sentence"] = plain_sentence(sentence["sentence"])
        ends.append({**end, "sentence": sentence})
    return ends


def named_ends(ends: list[dict]) -> list[dict]:
    out = []
    for end in ends:
        original = end.get("name") or ""
        forms = end.get("forms") or node_forms(original)
        if not forms:
            continue
        item = dict(end)
        item["name"] = forms[0]
        item["forms"] = forms
        out.append(item)
    return out


def run(out_dir: Path, *, target: str, mesh: str, as_of: date | None, ask=ask_model, clients: LiveClients | None = None, background: dict | None = None, direct: bool = False, from_direct: bool = False) -> dict:
    _load_dotenv()
    clients = clients or LiveClients()
    out_dir.mkdir(parents=True, exist_ok=True)
    sentence_path = out_dir / "sentences.json"
    lexicon_path = out_dir / "lexicon.json"
    target_lexicon_path = out_dir / "target-writings.json"
    resume = sentence_path.is_file() and lexicon_path.is_file()
    # A direct resume keeps the saved sentences and does not reopen MeSH or UniProt.
    if (direct or from_direct) and resume:
        preferred = ""
        raw_names = [target]
        mesh_names = []
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            preferred_future = pool.submit(clients.mesh_preferred_name, mesh)
            entry_future = pool.submit(clients.mesh_entry_terms, mesh)
            class_future = pool.submit(target_class, clients, target)
            preferred = preferred_future.result()
            raw_names = class_future.result()
            mesh_names = merge_terms([preferred], entry_future.result())
    from intake import background_record, live_mesh, live_uniprot
    returned = literature_names(raw_names)
    given_record = background_record(background) if background else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        question_future = None if resume else pool.submit(questions, ask, target, preferred, mesh_names)
        lexicon_future = None if resume else pool.submit(disease_writings, ask, preferred, mesh_names)
        if given_record is None and not ((direct or from_direct) and resume):
            found = ask_target_ids(ask_messages, target, live_mesh, live_uniprot)
            if found.get("mesh") or found.get("uniprot"):
                given_record = {"mesh": found.get("mesh") or "", "uniprot": found.get("uniprot") or ""}
            returned = merge_terms(returned, literature_names(found.get("returned") or []))
            print(
                f"target record mesh {((given_record or {}).get('mesh') or '')} queries {len(found.get('queries') or [])}",
                flush=True,
            )
        names = target_writings_for_run(target_lexicon_path, resume, ask, target, returned)
        print(f"target writings {len(names)}", flush=True)
        if resume:
            rows = recut_rows(json.loads(sentence_path.read_text(encoding="utf-8")))
            sentence_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            terms = json.loads(lexicon_path.read_text(encoding="utf-8"))
            if not preferred:
                if not terms:
                    raise SystemExit("stop: saved disease writings are empty")
                preferred = terms[0]
            print(f"resume sentences {len(rows)} lexicon {len(terms)}", flush=True)
        else:
            target_question, disease_question = question_future.result()
            terms = lexicon_future.result()
            target_words, disease_words = opening_words(names, terms)
            print(f"questions ready {mesh} writings target {len(target_words)} disease {len(disease_words)}", flush=True)
            with ThreadPoolExecutor(max_workers=2) as pool_papers:
                target_future = pool_papers.submit(
                    clients._bohrium, target_question, as_of, keep=SIDE_CAP, words=target_words,
                )
                disease_future = pool_papers.submit(
                    clients._bohrium, disease_question, as_of, keep=SIDE_CAP, words=disease_words,
                )
                target_papers = target_future.result()
                disease_papers = disease_future.result()
            print(f"papers target {len(target_papers)} disease {len(disease_papers)}", flush=True)
            if not target_papers or not disease_papers:
                raise SystemExit("stop: a literature side returned 0 papers")
            rows = sentence_rows(target_papers, target_words, "target") + sentence_rows(disease_papers, disease_words, "disease")
            sentence_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            lexicon_path.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"sentences {len(rows)} lexicon {len(terms)}", flush=True)
    target_rows = [row for row in rows if row["side"] == "target"]
    disease_rows = [row for row in rows if row["side"] == "disease"]
    if not target_rows or not disease_rows:
        raise SystemExit("stop: a sentence side is empty")
    seen_concept: dict[str, dict] = {}
    concept_lock = Lock()

    def cached_concept(name: str) -> dict:
        key = " ".join(str(name).split()).casefold()
        with concept_lock:
            if key not in seen_concept:
                seen_concept[key] = lookup_concept(clients, name)
            return seen_concept[key]

    from intake import live_mesh, live_uniprot

    def with_lookup(selected: list[dict]) -> list[dict]:
        def one(end: dict) -> dict:
            record = end.get("record") or {}
            if record.get("mesh") or record.get("uniprot"):
                return end
            return with_record(end, cached_concept(end.get("name") or ""))

        if len(selected) <= 1:
            return [one(end) for end in selected]
        with ThreadPoolExecutor(max_workers=min(AXIS_WORKERS, len(selected))) as pool:
            return list(pool.map(one, selected))

    def judge_side(selected: list[dict], locked: list[dict] | None = None) -> tuple[list[dict], dict]:
        return judge_identity(
            ask_messages,
            with_lookup(selected),
            target=target,
            disease=preferred,
            mesh=mesh,
            mesh_search=live_mesh,
            uniprot_search=live_uniprot,
            locked=locked,
            target_record=given_record,
        )

    ends_path = out_dir / "ends.json"
    if from_direct:
        direct_file = out_dir / "direct.json"
        if not direct_file.is_file():
            raise SystemExit("stop: direct.json is missing")
        starts, disease_ends = direct_ends(json.loads(direct_file.read_text(encoding="utf-8")))
        if not starts or not disease_ends:
            raise SystemExit("stop: saved direct ends are empty")
        target_report = {}
        disease_report = {}
        print(f"from direct target {len(starts)} disease {len(disease_ends)}", flush=True)
    elif ends_path.is_file():
        saved = json.loads(ends_path.read_text(encoding="utf-8"))
        target_ends = saved.get("target") or []
        disease_ends = saved.get("disease") or []
        if not target_ends or not disease_ends:
            raise SystemExit("stop: saved ends are empty")
        print(f"resume ends target {len(target_ends)} disease {len(disease_ends)}", flush=True)
        target_ends, target_report = side_after_merge(target_ends, direct=direct, judge=judge_side)
        disease_ends, disease_report = side_after_merge(disease_ends, direct=direct, judge=judge_side)
    else:
        partial_path = out_dir / "ends-partial.json"
        resumed_target = None
        if partial_path.is_file():
            resumed_target = saved_target_ends(json.loads(partial_path.read_text(encoding="utf-8")))

        def load_target() -> tuple[list[dict], dict]:
            if resumed_target:
                print(f"resume target ends {len(resumed_target)}", flush=True)
                selected = resumed_target
            else:
                selected = select_ends(
                    ask, target_rows, kind="target action", target=target, disease=preferred,
                    target_names=names, disease_names=terms,
                )
            return side_after_merge(selected, direct=direct, judge=judge_side)

        def load_disease() -> tuple[list[dict], dict]:
            selected = select_ends(
                ask, disease_rows, kind="disease pathology", target=target, disease=preferred,
                target_names=names, disease_names=terms,
            )
            return side_after_merge(selected, direct=direct, judge=judge_side)

        with ThreadPoolExecutor(max_workers=2) as pool:
            target_future = pool.submit(load_target)
            disease_future = pool.submit(load_disease)
            try:
                target_ends, target_report = target_future.result()
            except SelectionStopped as exc:
                (out_dir / "ends-reply.txt").write_text(exc.reply, encoding="utf-8")
                (out_dir / "ends-partial.json").write_text(
                    json.dumps({"kind": exc.kind, "kept": exc.picked}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print("stop reply " + " | ".join(exc.reply.splitlines())[:400], flush=True)
                raise SystemExit(f"stop: {exc}") from exc
            try:
                disease_ends, disease_report = disease_future.result()
            except SelectionStopped as exc:
                (out_dir / "ends-reply.txt").write_text(exc.reply, encoding="utf-8")
                (out_dir / "ends-partial.json").write_text(
                    json.dumps({"target": target_ends, "kind": exc.kind, "kept": exc.picked}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print("stop reply " + " | ".join(exc.reply.splitlines())[:400], flush=True)
                raise SystemExit(f"stop: {exc}") from exc
        reply_path = out_dir / "ends-reply.txt"
        if reply_path.is_file():
            reply_path.unlink()
    if not from_direct:
        ends_path.write_text(
            json.dumps({"target": target_ends, "disease": disease_ends}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / "identity.json").write_text(
            json.dumps({"target": target_report, "disease": disease_report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"ends target {len(target_ends)} disease {len(disease_ends)}", flush=True)
        if not disease_ends:
            raise SystemExit("stop: disease ends are empty")
        starts = choose_starts(ask, target_ends, target=target, disease=preferred)
        print(f"starts {len(starts)}", flush=True)
        if direct:
            disease_direct = choose_starts(
                ask, disease_ends, target=target, disease=preferred, side="disease",
            )
            print(f"disease direct {len(disease_direct)}", flush=True)
            target_words, disease_words = opening_words(names, terms)

            def brief(rows: list[dict]) -> list[dict]:
                return [{"name": row.get("name") or "", "sentence": _end_sentence(row)} for row in rows]

            (out_dir / "direct.json").write_text(
                json.dumps(
                    {
                        "target_words": target_words,
                        "disease_words": disease_words,
                        "target": brief(starts),
                        "disease": brief(disease_direct),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return {"target": starts, "disease": disease_direct, "terms": terms}
    shown = []
    missed: list[dict] = []
    if not disease_ends:
        raise SystemExit("stop: disease ends are empty")
    identity_block = removed_writings(target_report) + removed_writings(disease_report)

    def outer_one(item: tuple) -> None:
        end, anchor, forms = item
        end["evidence"] = outer_sentences(
            clients, anchor, end["name"], end.get("sentence"), as_of, forms, end.get("forms"), ask,
        )
        print(f"outer {anchor[:40]} / {end['name'][:40]} {len(end['evidence'])}", flush=True)

    outer_jobs = [(end, target, names) for end in starts] + [(end, preferred, terms) for end in disease_ends]
    if outer_jobs:
        with ThreadPoolExecutor(max_workers=min(AXIS_WORKERS, len(outer_jobs))) as pool:
            list(pool.map(outer_one, outer_jobs))
    workers = min(AXIS_WORKERS, max(1, len(starts)))

    groups = WritingGroups()
    groups.add(names)
    groups.add(terms)
    ledger: list[dict] = []

    def judge_proposal(name: str) -> dict:
        _kept, report = judge_identity(
            ask_messages,
            [{"name": name, "record": {}}],
            target=target,
            disease=preferred,
            mesh=mesh,
            mesh_search=live_mesh,
            uniprot_search=live_uniprot,
            locked=disease_ends,
            target_record=given_record,
        )
        if report.get("missing"):
            return {"action": "keep"}
        for item in report.get("applied") or []:
            named = {_clean(part).casefold() for part in (item.get("names") or [])}
            if _clean(name).casefold() not in named:
                continue
            if item.get("role") == "target":
                return {"action": "drop", "writings": removed_writings({"applied": [item]})}
            if item.get("role") == "endpoint":
                return {"action": "arrive", "writings": removed_writings({"applied": [item]})}
            display = _clean(item.get("display"))
            if item.get("role") == "node" and display and display.casefold() != _clean(name).casefold():
                return {"action": "join", "display": display}
        return {"action": "keep"}

    def one_end(end: dict) -> tuple[list[dict], list[dict]]:
        paths, gaps = walk_axes(
            ask_messages, clients, target, end, disease_ends, preferred, as_of, names, cached_concept, ledger, identity_block,
            judge_name=judge_proposal, groups=groups, disease_names=terms,
        )
        return [assemble_axis(target, preferred, path, disease_ends, names) for path in paths], gaps

    left_forms = (literature_names(names) or [target])[:FORM_CAP]
    right_forms = terms or [preferred]

    def load_one(extra: str) -> list[dict]:
        try:
            found, _gap = form_sentences(clients, left_forms, right_forms, as_of, ask, extra)
            return found
        except Exception as exc:
            print(f"direct skip {extra or 'pair'}: {exc}", flush=True)
            return []

    with ThreadPoolExecutor(max_workers=2) as direct_pool:
        pair_future = direct_pool.submit(load_one, "")
        trial_future = direct_pool.submit(load_one, "Clinical Trial[pt]")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one_end, end) for end in starts]
            try:
                for fut in as_completed(futures):
                    axes, gaps = fut.result()
                    shown.extend(axes)
                    missed.extend(gaps)
                    for axis in axes:
                        print(f"axis {axis['hops'][1]['left'][:60]} complete", flush=True)
            except Exception:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        pair_rows = pair_future.result()
        trial_rows = trial_future.result()
    label_axes(shown, ask)
    shown, unlabeled_axes = axes_with_evidence(shown)
    missed.extend(unlabeled_axes)
    joins = disease_joins(shown, preferred, disease_ends, terms)
    if joins:
        label_axes([{"hops": joins}], ask)
        joins = [hop for hop in joins if _evidence_rows(hop.get("sentences"))]
    if pair_rows:
        carrier = [{"hops": [{
            "left": target, "right": preferred,
            "left_forms": left_forms, "right_forms": right_forms,
            "sentences": pair_rows,
        }]}]
        label_axes(carrier, ask)
        pair_rows = carrier[0]["hops"][0]["sentences"]
    if trial_rows:
        carrier = [{"hops": [{
            "left": target, "right": preferred,
            "left_forms": left_forms, "right_forms": right_forms,
            "sentences": trial_rows,
        }]}]
        label_axes(carrier, ask)
        trial_rows = carrier[0]["hops"][0]["sentences"]
    note = ask(status_prompt(shown, pair_rows, trial_rows, joins))
    (out_dir / "axes.json").write_text(
        json.dumps({"axes": shown, "joins": joins, "incomplete": missed, "ledger": ledger, "status": note}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "report.html").write_text(present_html(target, preferred, shown, note, joins), encoding="utf-8")
    (out_dir / "report.md").write_text(report_md(target, preferred, shown, note, missed, joins), encoding="utf-8")
    print(f"complete axes {len(shown)} incomplete {len(missed)}", flush=True)
    return {"axes": shown, "terms": terms, "rows": rows}


def _self_check() -> None:
    assert pair_query("Left Name", "Right Name") == '("Left Name"[tiab]) AND ("Right Name"[tiab])'
    assert node_name("alpha step (marker)") == "alpha step"
    assert node_name("alpha step (a (b))") == "alpha step"
    assert node_name("alpha (marker) step") == "alpha (marker) step"
    assert node_name("alpha step （marker）") == "alpha step"
    assert node_name("(marker)") == ""
    paren_library = [{"sentence": "Marker drives alpha step (marker) today."}]
    assert copied_spans("1\talpha step (marker)", paren_library, [])[0]["name"] == "alpha step (marker)"
    assert "IgAN" in merge_terms(
        ["Glomerulonephritis, IGA"],
        _lexicon_lines("IgAN\tGlomerulonephritis, IGA\nTERM\tBerger"),
    )
    def ask_disease_name(prompt: str) -> str:
        assert "1. Coronary Artery Disease" in prompt and "one number from the list above" in prompt
        assert "The program keeps it." in prompt and "opening search" in prompt
        return "NODE\nCAD\nNAME\n1\nABBR\nCAD\nSAME\nnone"

    assert disease_writings(
        ask_disease_name,
        "Coronary Disease",
        ["Coronary Artery Disease", "Coronary Disease"],
    ) == ["Coronary Disease", "Coronary Artery Disease", "CAD"]
    iga_words = disease_writings(
        lambda prompt: "NODE\nIgAN\nNAME\n2\nABBR\nIgAN\nSAME\nBerger Disease",
        "Glomerulonephritis, IGA",
        ["Glomerulonephritis, IGA", "IGA Nephropathy", "Berger Disease"],
    )
    assert iga_words == ["Glomerulonephritis, IGA", "IGA Nephropathy", "IgAN"]
    assert "Berger Disease" not in iga_words
    cad_terms = [
        "Coronary Artery Disease",
        "Artery Disease, Coronary",
        "Artery Diseases, Coronary",
        "Coronary Artery Diseases",
    ]

    def ask_cad(name_line: str):
        def ask(prompt: str) -> str:
            assert "or none" in prompt
            assert "NAME is one synonym" in prompt
            head, listed = prompt.split("1. ", 1)
            assert "Artery Disease, Coronary" not in head
            return (
                "NODE\nCoronary Artery Disease\nNAME\n"
                + name_line
                + "\nABBR\nCAD\nSAME\nnone"
            )
        return ask

    assert disease_writings(ask_cad("none"), "Coronary Artery Disease", cad_terms) == [
        "Coronary Artery Disease",
        "CAD",
    ]
    assert disease_writings(ask_cad("1"), "Coronary Artery Disease", cad_terms) == [
        "Coronary Artery Disease",
        "CAD",
    ]
    assert disease_writings(ask_cad("2"), "Coronary Artery Disease", cad_terms) == [
        "Coronary Artery Disease",
        "Artery Disease, Coronary",
        "CAD",
    ]
    assert disease_writings(ask_cad("4"), "Coronary Artery Disease", cad_terms) == [
        "Coronary Artery Disease",
        "Coronary Artery Diseases",
        "CAD",
    ]
    def ask_target_name(prompt: str) -> str:
        assert "Target: CRP" in prompt and "The program keeps it." in prompt and "opening search" in prompt
        return "NODE\nCRP\nNAME\n1\nABBR\nCRP\nSAME\nPTX1"

    crp_words = target_writings(
        ask_target_name,
        "CRP",
        ["C-reactive protein", "PTX1"],
    )
    assert crp_words == ["CRP", "C-reactive protein"]
    assert "PTX1" not in crp_words
    assert target_writings(
        lambda prompt: "NODE\nCRP\nNAME\nC-reactive protein\nABBR\nnone\nSAME\nnone",
        "CRP",
        ["C-reactive protein"],
    ) == ["CRP"]
    assert target_writings(
        lambda prompt: "NODE\nCRP\nNAME\nPTX1\nABBR\nCRP\nSAME\nnone",
        "CRP",
        ["C-reactive protein"],
    ) == ["CRP"]
    assert writing_sections("NODE Kv1.3 NAME 2 ABBR none SAME none")["ok"] is False
    assert target_writings(
        lambda prompt: "NODE\nGLP-1\nNAME\n2\nABBR\nnone\nSAME\nnone",
        "GLP-1",
        ["GLP-1", "glucagon-like peptide 1"],
    ) == ["GLP-1", "glucagon-like peptide 1"]
    for given, other in (
        ("Kv1.3", "KCNA3"),
        ("GLP-1", "glucagon-like peptide 1"),
        ("KCa3.1", "KCNN4"),
        ("ENPP-3", "CD203c"),
        ("KCNA3", "Kv1.3"),
    ):
        reply = f"NODE {given}\nNAME 2\nABBR none\nSAME none"
        assert target_writings(lambda prompt, reply=reply: reply, given, [given, other]) == [given, other]
    library = [{"sentence": "Marker drives alpha step in cells.", "side": "target"}]
    assert copied_spans("1\talpha step", library, ["Marker"])[0]["name"] == "alpha step"
    assert copied_spans("no such span\n1\talpha step", library, ["Marker"])[0]["name"] == "alpha step"
    assert copied_spans("1\tno such span", library, ["Marker"])[0]["name"] == "no such span"
    assert copied_spans("Marker drives alpha step today.", library, ["Marker"]) == []
    assert copied_spans("1\tMarker", library, ["Marker"]) == []
    hyphen_rows = [{"sentence": "blockers reduce IL\u20101beta and TNF\u2010alpha."}]
    assert [row["name"] for row in copied_spans("1\tIL-1beta\n1\tTNF-alpha", hyphen_rows, [])] == [
        "IL-1beta",
        "TNF-alpha",
    ]
    assert linking_sentences(
        [{"sentence": "activation is mentioned."}], "channel opening", "mesangial deposition"
    ) == []
    start = {"name": "channel opening", "sentence": {"sentence": "TargetA drives channel opening.", "pmid": "1"}}
    pathology = {"name": "mesangial deposition", "sentence": {"sentence": "DiseaseB follows mesangial deposition.", "pmid": "2"}}

    class Clients:
        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            names = re.findall(r'"([^"]+)"', query)
            text = f"{names[0]} links {names[1]}." if len(names) >= 2 else "empty"
            return [Paper("9", "t", text, None, "pubmed", "", "9", "")]

    def ask_join(messages: list[dict]) -> str:
        prompt = messages[-1]["content"]
        if "The whole reply is one line" in prompt:
            return "related"
        if "Propose" in prompt:
            return "mesangial deposition"
        if "complete axes" in prompt or prompt.startswith("TASK status"):
            return "The middle sentence names both nodes."
        return "noted"

    paths, joined_gaps = walk_axes(ask_join, Clients(), "TargetA", start, [pathology], "DiseaseB", None)
    assert len(paths) == 1 and joined_gaps == []
    shown = assemble_axis("TargetA", "DiseaseB", paths[0], [pathology])
    original = {"sentence": "Bohrium keeps TargetA with channel opening.", "pmid": "1"}

    class OuterClients:
        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            names = re.findall(r'"([^"]+)"', query)
            both = f"{names[0]} binds {names[1]}." if len(names) >= 2 else "empty"
            one = f"Only {names[0]} is named." if names else "empty"
            return [
                Paper("8", "t", both, None, "pubmed", "", "8", ""),
                Paper("7", "t", one, None, "pubmed", "", "7", ""),
            ]

    supplemented = outer_sentences(OuterClients(), "TargetA", "channel opening", original, None)
    assert supplemented[0]["pmid"] == "1"
    assert len(supplemented) == 2 and "binds" in supplemented[1]["sentence"]
    empty_client = type("Empty", (), {"search_papers": lambda *a, **k: []})()
    empty = outer_sentences(empty_client, "NoHit", "channel opening", original, None)
    assert empty == [original]
    assert [hop["right"] for hop in shown["hops"]] == [
        "channel opening",
        "mesangial deposition",
    ]
    joins = disease_joins([shown], "DiseaseB", [pathology])
    assert joins[0]["left"] == "mesangial deposition" and joins[0]["right"] == "DiseaseB"
    assert len(disease_joins([shown, shown], "DiseaseB", [pathology])) == 1
    joined = present_html("TargetA", "DiseaseB", [shown], "The route holds.", joins)
    assert "到病名" not in joined and "<h2>Summary</h2>" in joined
    assert joined.index("<h2>Summary</h2>") < joined.index('class="overview"')
    assert "<h2>Axis 1</h2>" in joined and "<h3>mesangial deposition → DiseaseB</h3>" in joined
    assert "pubmed.ncbi.nlm.nih.gov/2/" in joined
    joined_text = report_md("TargetA", "DiseaseB", [shown], "The route holds.", None, joins)
    assert "## To the disease" not in joined_text and "## Summary" in joined_text
    assert joined_text.index("## Summary") < joined_text.index("## 1.")
    assert "mesangial deposition → DiseaseB" in joined_text
    page = present_html("TargetA", "DiseaseB", [shown], "## Axis 1\n- supports both\n  - nested point")
    text = report_md("TargetA", "DiseaseB", [shown], "note")
    assert "channel opening links mesangial deposition." in page
    assert "channel opening links mesangial deposition." in text
    assert "pmid" not in text or "9" in text
    assert "<h3>Axis 1</h3>" in page
    assert 'style="margin-left:1em"' in page
    shared = [
        {"hops": [
            {"left": "TargetA", "right": "alpha step", "sentences": [{"sentence": "a"}]},
            {"left": "alpha step", "right": "Marker", "sentences": [{"sentence": "b"}]},
            {"left": "Marker", "right": "DiseaseB", "sentences": [{"sentence": "c"}]},
        ]},
        {"hops": [
            {"left": "TargetA", "right": "other step", "sentences": [{"sentence": "d"}]},
            {"left": "other step", "right": "Marker", "sentences": [{"sentence": "e"}]},
            {"left": "Marker", "right": "DiseaseB", "sentences": [{"sentence": "f"}]},
        ]},
    ]
    merged = present_html("TargetA", "DiseaseB", shared, "")
    overview = merged.split('class="overview"', 1)[1].split("</section>", 1)[0]
    assert overview.count(">Marker<") == 1
    assert ">alpha step<" in overview and ">other step<" in overview
    folded = present_html("TargetA", "DiseaseB", [
        {"hops": [
            {"left": "TargetA", "right": "Alpha Step", "sentences": [{"sentence": "a"}]},
            {"left": "Alpha Step", "right": "DiseaseB", "sentences": [{"sentence": "b"}]},
        ]},
        {"hops": [
            {"left": "TargetA", "right": "alpha step", "sentences": [{"sentence": "c"}]},
            {"left": "alpha step", "right": "DiseaseB", "sentences": [{"sentence": "d"}]},
        ]},
    ], "")
    folded_map = folded.split('class="overview"', 1)[1].split("</section>", 1)[0]
    assert folded_map.count("Alpha Step") == 1
    assert ">alpha step<" not in folded_map
    same = present_html("TargetA", "DiseaseB", [
        {"hops": [
            {"left": "TargetA", "right": "TargetA", "sentences": [{"sentence": "a"}]},
            {"left": "TargetA", "right": "DiseaseB", "sentences": [{"sentence": "b"}]},
        ]},
    ], "")
    same_map = same.split('class="overview"', 1)[1].split("</section>", 1)[0]
    assert "&quot;n0&quot;, &quot;n0&quot;" not in same_map
    assert "&quot;n0&quot;, &quot;n1&quot;" in same_map
    distinct = present_html("TargetA", "DiseaseB", [
        {"hops": [
            {"left": "TargetA", "right": "T cell activation", "sentences": [{"sentence": "a"}]},
            {"left": "T cell activation", "right": "DiseaseB", "sentences": [{"sentence": "b"}]},
        ]},
        {"hops": [
            {"left": "TargetA", "right": "T-cell activation", "sentences": [{"sentence": "c"}]},
            {"left": "T-cell activation", "right": "DiseaseB", "sentences": [{"sentence": "d"}]},
        ]},
    ], "")
    distinct_map = distinct.split('class="overview"', 1)[1].split("</section>", 1)[0]
    assert ">T cell activation<" in distinct_map and ">T-cell activation<" in distinct_map

    def ask_long(messages: list[dict]) -> str:
        prompt = messages[-1]["content"]
        if "The whole reply is one line" in prompt:
            return "related"
        if "Propose" not in prompt:
            return ""
        body = prompt.split("Path so far:\n", 1)[1].split("Propose", 1)[0]
        depth = len([line for line in body.splitlines() if line.strip()])
        return f"node{depth}"

    long_start = {**start, "evidence": [{"sentence": "node1 node2 node3 node4 node5 node6 node7"}]}
    found, missed = walk_axes(ask_long, Clients(), "TargetA", long_start, [pathology], "DiseaseB", None)
    assert found == []
    assert missed and missed[-1]["stop"] == "six intermediates"

    class Quiet:
        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            return []

    def ask_gap(messages: list[dict]) -> str:
        if "Propose" in messages[-1]["content"]:
            assert "Fewer is correct" not in messages[-1]["content"]
            assert "each distinct next node" in messages[-1]["content"]
            assert "Two different objects are two lines" in messages[-1]["content"]
            assert "Do not invent another line" not in messages[-1]["content"]
            assert "other writings of that same name" not in messages[-1]["content"]
            return "missing step"
        return ""

    gap_start = {**start, "evidence": [{"sentence": "A missing step remains."}]}
    gaps_found, gaps = walk_axes(ask_gap, Quiet(), "TargetA", gap_start, [pathology], "DiseaseB", None)
    assert gaps_found == []
    assert gaps[0]["stop"] == "no literature" and gaps[0]["proposed"] == "missing step"
    assert gaps[0]["steps"][0]["sentences"][0]["sentence"] == "TargetA drives channel opening."
    retry_calls = {"n": 0}

    def ask_retry(messages: list[dict]) -> str:
        if "Propose" not in messages[-1]["content"]:
            return ""
        retry_calls["n"] += 1
        if "no literature from" in messages[-1]["content"]:
            return "second step"
        return "first step"

    retry_found, retry_gaps = walk_axes(ask_retry, Quiet(), "TargetA", gap_start, [pathology], "DiseaseB", None)
    assert retry_found == []
    assert retry_calls["n"] == 2
    assert [item["proposed"] for item in retry_gaps] == ["first step", "second step"]
    pair_calls = {"n": 0}

    def ask_pair(messages: list[dict]) -> str:
        if "Propose" not in messages[-1]["content"]:
            return ""
        pair_calls["n"] += 1
        return "one step\nanother step"

    walk_axes(ask_pair, Quiet(), "TargetA", gap_start, [pathology], "DiseaseB", None)
    assert pair_calls["n"] == 1
    same_names = WritingGroups()
    same_names.add(["Glomerulonephritis, IGA", "IgAN"])
    same_names.add(["C5b-6", "C5b6"])
    assert same_names.members("IgAN") == ["Glomerulonephritis, IGA", "IgAN"]
    assert same_names.members("C5b6") == ["C5b-6", "C5b6"]
    assert "Glomerulonephritis, IGA" not in same_names.members("C5b6")
    assert "missing step" not in present_html("TargetA", "DiseaseB", [], "")
    assert "missing step" in report_md("TargetA", "DiseaseB", [], "", gaps)

    def ask_paren(messages: list[dict]) -> str:
        if "The whole reply is one line" in messages[-1]["content"]:
            return "related"
        if "Propose" in messages[-1]["content"]:
            return "mesangial deposition (marker)"
        return ""

    paren_end = {"name": "mesangial deposition (marker)", "sentence": {"sentence": "kept aside"}}
    reached, paren_gaps = walk_axes(ask_paren, Clients(), "TargetA", start, [paren_end], "DiseaseB", None)
    assert paren_gaps == []
    assert reached and reached[0][-1]["name"] == "mesangial deposition"
    assert node_forms("alpha step (marker)") == ["alpha step", "marker"]
    assert node_forms("alpha (marker) step") == ["alpha (marker) step"]
    alias_end = named_ends([{"name": "mesangial deposition (marker)", "sentence": {"sentence": "kept aside"}}])[0]

    class AliasOnly:
        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            names = re.findall(r'"([^"]+)"', query)
            if len(names) >= 2 and "marker" in {name.casefold() for name in names[1:]}:
                return [Paper("8", "t", f"{names[0]} links marker.", None, "pubmed", "", "8", "")]
            return []

    def ask_body(messages: list[dict]) -> str:
        if "The whole reply is one line" in messages[-1]["content"]:
            return "related"
        if "Propose" in messages[-1]["content"]:
            return "mesangial deposition"
        return ""

    alias_paths, alias_gaps = walk_axes(ask_body, AliasOnly(), "TargetA", start, [alias_end], "DiseaseB", None)
    assert alias_gaps == []
    assert alias_paths[0][-1]["name"] == "mesangial deposition"
    assert "links marker." in alias_paths[0][-1]["sentences"][0]["sentence"]
    item = {"name": "alpha step", "sentence": {"sentence": "Marker drives alpha step in cells."}}
    assert chosen_rows("1. alpha step", [item])[0]["name"] == "alpha step"
    assert chosen_rows("alpha step", [item]) == []
    def ask_group(prompt: str) -> str:
        assert prompt.startswith("TASK objects")
        assert "They are not extra rules" in prompt
        assert "1. Node: human T cell activation" in prompt
        assert "A line under LINES is only that number." in prompt
        assert "IgA deposition" not in prompt
        return (
            "NAME\nT cell activation\nLINES\n1\n2. rewritten\n"
            "NAME\nIL-10\nLINES\n3\n"
        )

    grouped = group_objects(ask_group, [
        {"name": "human T cell activation", "forms": ["human T cell activation"], "sentence": {"sentence": "human cells"}},
        {"name": "T cell activation", "forms": ["T cell activation"], "sentence": {"sentence": "activation"}},
        {"name": "IL-10", "forms": ["IL-10"], "sentence": {"sentence": "IL-10 rises"}},
        {"name": "edema", "forms": ["edema"], "sentence": {"sentence": "edema formed"}},
    ], kind="target action")
    assert [(row["name"], row["source"]) for row in grouped] == [
        ("T cell activation", "merged"),
        ("IL-10", "merged"),
        ("edema", "extracted"),
    ]
    assert [item["name"] for item in grouped[0]["members"]] == ["human T cell activation", "T cell activation"]
    six = [{"name": f"node {i}", "sentence": {"sentence": f"sentence {i}"}} for i in range(6)]
    calls = {"n": 0}

    def ask_starts(prompt: str) -> str:
        calls["n"] += 1
        assert "Fewer is correct" in prompt and "6. node 5" in prompt
        assert "its destination is the disease" in prompt
        assert "One line is one number from the list." in prompt
        return "2\n5. not a name\nmade up"

    assert [row["name"] for row in choose_starts(ask_starts, six[:3], target="T", disease="D")] == [
        "node 0", "node 1", "node 2",
    ]
    assert calls["n"] == 0
    assert [row["name"] for row in choose_starts(ask_starts, six, target="T", disease="D")] == ["node 1", "node 4"]
    assert choose_starts(lambda prompt: "nope", six, target="T", disease="D") == []

    def ask_disease_cap(prompt: str) -> str:
        assert "on the disease side" in prompt and "pathology of this disease" in prompt
        assert "its destination is the disease" in prompt
        assert "on the target side" not in prompt
        return "1\n4"

    assert [row["name"] for row in choose_starts(
        ask_disease_cap, six, target="T", disease="D", side="disease",
    )] == ["node 0", "node 3"]
    library_calls = []

    def judge_library(rows):
        library_calls.append(rows[0]["name"])
        return rows, {"judged": True}

    kept_side, kept_report = side_after_merge(
        [{"name": "cytokine production"}], direct=True, judge=judge_library,
    )
    assert kept_side[0]["name"] == "cytokine production" and kept_report == {} and library_calls == []
    judged_side, judged_report = side_after_merge(
        [{"name": "cytokine production"}], direct=False, judge=judge_library,
    )
    assert judged_side[0]["name"] == "cytokine production" and judged_report == {"judged": True}
    assert library_calls == ["cytokine production"]

    def ask_disease_group(prompt: str) -> str:
        assert "IgA deposition stays IgA deposition" in prompt
        return ""

    assert group_objects(
        ask_disease_group,
        [{"name": "edema", "forms": ["edema"], "sentence": {"sentence": "edema"}}],
        kind="disease pathology",
    )[0]["source"] == "extracted"
    split_calls = {"n": 0}

    def ask_split(prompt: str) -> str:
        split_calls["n"] += 1
        if "Sentence:" in prompt:
            if "1. Node: human T cell activation\n" in prompt:
                assert "1. Node: T-cell activation\n" not in prompt
                return "NAME\nT cell activation\nLINES\n1\n"
            assert "1. Node: T-cell activation\n" in prompt
            return "NAME\nT-cell activation\nLINES\n1\n"
        assert "Sentence:" not in prompt
        assert "separate batches" in prompt
        assert "1. Node: T cell activation" in prompt and "2. Node: T-cell activation" in prompt
        return "NAME\nT cell activation\nLINES\n1\n2\n"

    split = group_objects(ask_split, [
        {"name": "human T cell activation", "forms": ["human T cell activation"], "sentence": {"sentence": "human cells"}},
        {"name": "T-cell activation", "forms": ["T-cell activation"], "sentence": {"sentence": "T-cell activation"}},
    ], kind="target action", batch_chars=1)
    assert split_calls["n"] == 3
    assert [row["name"] for row in split] == ["T cell activation"]
    assert [item["name"] for item in split[0]["members"]] == ["human T cell activation", "T-cell activation"]
    assert select_ends(lambda prompt: "\n", library, kind="disease pathology", target="Marker", disease="DiseaseB") == []
    cut = recut_rows([
        {"side": "disease", "doi": "10.1/a", "sentence": "Marker drives alpha\nstep today."},
        {"side": "disease", "doi": "10.1/a", "sentence": "Marker drives alpha step today."},
    ])
    assert len(cut) == 1 and cut[0]["sentence"] == "Marker drives alpha step today."
    assert target_named(cut[0]["sentence"], ["alpha step"])
    kept = saved_target_ends({
        "kind": "disease pathology",
        "target": [{"name": "alpha step", "sentence": {"sentence": "Marker drives alpha\nstep today."}}],
    })
    assert kept is not None and kept[0]["sentence"]["sentence"] == "Marker drives alpha step today."
    assert saved_target_ends({"kind": "target action", "kept": [{"name": "alpha step"}]}) is None
    assert select_ends(lambda prompt: "zzzz unmatched", library, kind="target action", target="TargetA", disease="DiseaseB") == []
    destination = {}

    def ask_destination(prompt: str) -> str:
        destination["prompt"] = prompt
        return ""

    assert select_ends(
        ask_destination, library, kind="target action", target="Kv1.3", disease="Glomerulonephritis, IGA",
    ) == []
    assert "Disease: Glomerulonephritis, IGA" in destination["prompt"]
    assert "whose destination is this disease" in destination["prompt"]
    assert "when the sentence does not state it" in destination["prompt"]
    assert select_ends(
        ask_destination, library, kind="disease pathology", target="Kv1.3", disease="Glomerulonephritis, IGA",
    ) == []
    assert "Target: Kv1.3" in destination["prompt"]
    assert "the disease end that this chain reaches" in destination["prompt"]
    kept_target, kept_disease = direct_ends({
        "target": [{"name": "T cell activation", "sentence": "Kv1.3 changes T cell activation."}],
        "disease": [
            {"name": "mesangial IgA deposition", "sentence": "IgAN is characterized by mesangial IgA deposition."},
            {"name": " ", "sentence": "skip"},
        ],
    })
    assert [row["name"] for row in kept_target] == ["T cell activation"]
    assert [row["name"] for row in kept_disease] == ["mesangial IgA deposition"]
    assert kept_target[0]["forms"] == ["T cell activation"]
    assert kept_target[0]["sentence"]["sentence"].startswith("Kv1.3")
    assert library_result(lambda query: (_ for _ in ()).throw(RuntimeError(query)), "C5") == {
        "error": "unavailable",
        "query": "C5",
    }
    assert library_result(lambda query: {"rows": []}, "IL4R") == {"rows": []}
    lipid = [{"sentence": "Patients show elevated low-density lipoprotein cholesterol today.", "side": "disease"}]

    def ask_lipid(prompt: str) -> str:
        if prompt.startswith("TASK name"):
            return "low-density lipoprotein cholesterol"
        return "1\televated low-density lipoprotein cholesterol"

    lipid_ends = select_ends(
        ask_lipid, lipid, kind="disease pathology", target="PCSK9", disease="Hypercholesterolemia",
        target_names=["PCSK9"],
    )
    assert lipid_ends[0]["name"] == "elevated low-density lipoprotein cholesterol"
    assert lipid_ends[0]["sentence"]["sentence"].startswith("Patients show elevated")
    channel = [{"sentence": "KCNA3 opens in T cells.", "side": "disease"}]

    def ask_channel(prompt: str) -> str:
        if prompt.startswith("TASK name"):
            return "KCNA3"
        return "1\tKCNA3 opens"

    channel_ends = select_ends(
        ask_channel, channel, kind="disease pathology", target="Kv1.3", disease="IgA nephropathy",
        target_names=["Kv1.3", "KCNA3"],
    )
    assert [end["name"] for end in channel_ends] == ["KCNA3 opens"]
    assert literature_names(["XDH", "P47989", "xanthine dehydrogenase/oxidase"]) == [
        "XDH",
        "xanthine dehydrogenase/oxidase",
    ]
    assert opening_words(
        ["CRP", "P02741", "C-reactive protein"],
        ["Coronary Artery Disease", "CAD"],
    ) == (["CRP", "C-reactive protein"], ["Coronary Artery Disease", "CAD"])
    try:
        opening_words(["P02741"], [])
    except RuntimeError:
        pass
    else:
        raise AssertionError("empty writings were sent")
    seen_forms = {"n": 0}

    def fetch_forms() -> list[str]:
        seen_forms["n"] += 1
        return ["IL-6", "interleukin-6"]

    form_cache: dict[str, list[str]] = {}
    form_lock = Lock()
    assert cached_list(form_cache, form_lock, "il-6", fetch_forms) == ["IL-6", "interleukin-6"]
    assert cached_list(form_cache, form_lock, "il-6", fetch_forms) == ["IL-6", "interleukin-6"]
    assert seen_forms["n"] == 1
    alias_papers = [Paper("1", "XDH", "Xanthine oxidase forms uric acid in tissue.", None, "bohrium")]
    assert sentence_rows(alias_papers, ["XDH"], "target") == []
    assert sentence_rows(alias_papers, literature_names(["XDH", "P47989", "xanthine oxidase"]), "target")[0]["sentence"].startswith("Xanthine oxidase")
    saved_disease = (
        "It is well known that the kidney expresses inflammasome genes, which can influence the "
        "development of some glomerulonephritis, such as lupus nephritis, ANCA glomerulonephritis, "
        "IgA nephropathy and anti-GBM nephropathy."
    )
    search_words = opening_words(["Kv1.3"], ["Glomerulonephritis, IGA"])
    assert search_words == (["Kv1.3"], ["Glomerulonephritis, IGA"])
    assert sentence_rows([Paper("d", "t", saved_disease, None, "bohrium")], search_words[1], "disease") == []
    three_words = ["Glomerulonephritis, IGA", "IgA nephropathy", "IgAN"]
    assert sentence_rows([Paper("d", "t", saved_disease, None, "bohrium")], three_words, "disease")
    assert sentence_rows([Paper("d", "t", "Children with IgAN were followed.", None, "bohrium")], search_words[1], "disease") == []
    assert sentence_rows(
        [Paper("d", "t", "Children with IgAN were followed.", None, "bohrium")],
        three_words,
        "disease",
    )
    assert target_named("Glomerulonephritis, IGA is the disease under study.", ["Glomerulonephritis, IGA"])
    assert not target_named("patients with glomerulonephritis, IgA were followed.", ["Glomerulonephritis, IGA"])
    assert not target_named("ANCA glomerulonephritis, IgA.", ["Glomerulonephritis, IGA"])
    assert not target_named("Psoriatic arthritis, rheumatoid factor was positive.", ["Arthritis, Rheumatoid"])
    assert target_named("Arthritis, Rheumatoid is treated with methotrexate.", ["Arthritis, Rheumatoid"])
    assert target_named("Kv1.3 current was recorded.", ["Kv1.3"])
    assert not target_named("Children with IgA nephropathy were followed.", ["IgAN"])
    norm_rows = [
        {"sentence": "TargetA roles include alpha clearance today.", "side": "target"},
        {"sentence": "Samples contain marker (AB) crystals today.", "side": "target"},
        {"sentence": "The activated memory marker cells were counted.", "side": "target"},
        {"sentence": "Marker drives alpha step in cells.", "side": "target"},
    ]

    def ask_norm(prompt: str) -> str:
        if prompt.startswith("TASK ends"):
            assert "At most" not in prompt and "downstream process or product" in prompt
            assert "a tab, and none" in prompt and "copy the product" not in prompt
            assert "two objects" in prompt and "Two molecules are one example" in prompt
        if prompt.startswith("TASK name"):
            raise AssertionError("rewrite was called")
        return "1\talpha clearance\n2\tmarker (AB) crystals\n3\tactivated memory marker cells\n4\talpha step"

    norm_ends = select_ends(
        ask_norm, norm_rows, kind="target action", target="TargetA", disease="DiseaseB",
        target_names=["TargetA"], disease_names=["DiseaseB"],
    )
    assert [row["name"] for row in norm_ends] == [
        "alpha clearance",
        "marker (AB) crystals",
        "activated memory marker cells",
        "alpha step",
    ]
    assert proposal_names("KCNA3\ncalcineurin", ["channel opening"], ["KCNA3", "Kv1.3"]) == ["calcineurin"]

    class Uni:
        def uniprot(self, name: str) -> dict:
            return {"uniprot_id": "P22001", "aliases": ["KCNA3", name]}

    assert target_class(Uni(), "Kv1.3") == ["Kv1.3", "P22001", "KCNA3"]

    class UniDown:
        def uniprot(self, name: str) -> dict:
            raise RuntimeError("down")

    assert target_class(UniDown(), "Kv1.3") == ["Kv1.3"]

    alias_start = {**start, "evidence": [{"sentence": "C-reactive protein was measured."}]}
    judged_names = []

    def ask_block(messages: list[dict]) -> str:
        if "The whole reply is one line" in messages[-1]["content"]:
            return "related"
        if "Propose" in messages[-1]["content"] and "Left: C-reactive protein" not in messages[-1]["content"]:
            return "C-reactive protein\nmesangial deposition"
        return ""

    def judge_alias(name: str) -> dict:
        judged_names.append(name)
        if name.casefold() == "c-reactive protein":
            return {"action": "drop", "writings": [name]}
        return {"action": "keep"}

    blocked_paths, blocked_gaps = walk_axes(
        ask_block, Clients(), "CRP", alias_start, [pathology], "DiseaseB", None,
        target_names=["CRP", "C-reactive protein"],
        judge_name=judge_alias,
    )
    assert judged_names == []
    assert [step["name"] for step in blocked_paths[0]] == [
        "channel opening",
        "mesangial deposition",
    ]
    assert blocked_gaps == []
    disease_words = ["Glomerulonephritis, IGA", "IgA nephropathy", "IgAN"]
    visible = [{"name": "mesangial deposition", "sentence": {"sentence": "selected earlier"}}]

    class ArriveClients:
        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            found = {name.casefold() for name in re.findall(r'"([^"]+)"', query)}
            if "iga production" in found and "iga nephropathy" in found:
                return [Paper("3", "t", "IgA production drives IgA nephropathy.", None, "pubmed", "", "3", "")]
            return []

    def ask_arrive(messages: list[dict]) -> str:
        prompt = messages[-1]["content"]
        if "The whole reply is one line" in prompt:
            head = prompt.split("Sentence:", 1)[0]
            if "IgA nephropathy" in head and "any one of them is the right end" in head:
                return "related"
            return "other"
        if "Propose" in prompt and "Left: IgA production" in prompt:
            return "IgA nephropathy\ncomplement activation"
        return ""

    arrived, arrive_gaps = walk_axes(
        ask_arrive, ArriveClients(), "Kv1.3",
        {"name": "IgA production", "sentence": {"sentence": "A new node."}},
        visible, "Glomerulonephritis, IGA", None,
        disease_names=disease_words,
    )
    assert [step["name"] for step in arrived[0]] == ["IgA production", "Glomerulonephritis, IGA"]
    assert "IgA nephropathy" in arrived[0][-1]["sentences"][0]["sentence"]
    assert all(item.get("stop") != "identity" for item in arrive_gaps)
    assert any(item.get("proposed") == "complement activation" and item.get("stop") == "no literature" for item in arrive_gaps)
    assert [row["name"] for row in visible] == ["mesangial deposition"]
    overlap = {"n": 0, "max": 0}
    overlap_lock = __import__("threading").Lock()

    def ask_overlap(prompt: str) -> str:
        if prompt.startswith("TASK name"):
            with overlap_lock:
                overlap["n"] += 1
                overlap["max"] = max(overlap["max"], overlap["n"])
            __import__("time").sleep(0.2)
            with overlap_lock:
                overlap["n"] -= 1
            return "alpha step"
        return "\n".join(f"cell {i} does alpha step" for i in range(4))

    overlap_rows = [{"sentence": f"cell {i} does alpha step today.", "side": "target"} for i in range(4)]
    select_ends(ask_overlap, overlap_rows, kind="target action", target="TargetA", disease="DiseaseB")
    assert overlap["max"] == 0
    assert overlap["n"] == 0

    def ask_clause(prompt: str) -> str:
        if prompt.startswith("TASK name"):
            raise AssertionError("rewrite was called")
        return "1\tdeficient activity of the enzyme porphobilinogen deaminase"

    clause_rows = [{
        "sentence": "Acute intermittent porphyria is characterized by deficient activity of the enzyme porphobilinogen deaminase.",
        "side": "disease",
    }]
    clause_ends = select_ends(
        ask_clause, clause_rows, kind="disease pathology", target="ALAS1",
        disease="Porphyria, Acute Intermittent", target_names=["ALAS1"],
    )
    assert clause_ends[0]["name"] == "deficient activity of the enzyme porphobilinogen deaminase"
    assert clause_ends[0]["pre"] == "deficient activity of the enzyme porphobilinogen deaminase"
    batch_overlap = {"n": 0, "max": 0}
    batch_lock = __import__("threading").Lock()

    def ask_batches(prompt: str) -> str:
        if prompt.startswith("TASK ends"):
            with batch_lock:
                batch_overlap["n"] += 1
                batch_overlap["max"] = max(batch_overlap["max"], batch_overlap["n"])
            time.sleep(0.15)
            with batch_lock:
                batch_overlap["n"] -= 1
            if "alpha one" in prompt:
                return "1\talpha one"
            return "1\talpha two"
        return ""

    batch_rows = [
        {"sentence": "alpha one " + ("a" * 8000), "side": "target"},
        {"sentence": "alpha two " + ("b" * 8000), "side": "target"},
    ]
    batch_ends = select_ends(
        ask_batches, batch_rows, kind="target action", target="TargetA", disease="DiseaseB",
    )
    assert batch_overlap["max"] > 1
    assert [row["name"] for row in batch_ends] == ["alpha one", "alpha two"]

    def ask_order(messages: list[dict]) -> str:
        content = messages[-1]["content"]
        if "The whole reply is one line" in content:
            return "related"
        if "Propose" not in content:
            return ""
        if "Left: channel opening" in content:
            return "slow step\nfast step"
        if "Left: slow step" in content:
            time.sleep(0.25)
        return ""

    order_start = {**start, "evidence": [{"sentence": "slow step and fast step were measured."}]}
    _found, order_missed = walk_axes(ask_order, Clients(), "TargetA", order_start, [pathology], "DiseaseB", None)
    assert [item["stop"] for item in order_missed] == ["no proposal", "no proposal"]
    assert [item["nodes"][-1] for item in order_missed] == ["slow step", "fast step"]
    calls = {"n": 0}

    class CountClient:
        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            calls["n"] += 1
            return [Paper("1", "t", "cache left links cache right.", None, "pubmed", "", "1", "")]

    counter = CountClient()
    assert pubmed_papers(counter, "cache left", "cache right", None)
    assert pubmed_papers(counter, "cache left", "cache right", None) == pubmed_papers(
        counter, "cache left", "cache right", None,
    )
    assert calls["n"] == 1

    kept_split, split_report = apply_identity(
        [
            {"name": "high-sensitivity C-reactive protein", "record": {"mesh": "D002097"}},
            {"name": "hs-CRP", "record": {"mesh": "D002097"}},
            {"name": "inflammation", "record": {"mesh": "D007249"}},
        ],
        [
            {"names": ["high-sensitivity C-reactive protein"], "display": "high-sensitivity C-reactive protein", "role": "node"},
            {"names": ["hs-CRP"], "display": "hs-CRP", "role": "node"},
        ],
    )
    assert [end["name"] for end in kept_split] == ["high-sensitivity C-reactive protein", "hs-CRP", "inflammation"]
    assert split_report["conflicts"] == []
    kept_target, target_report = apply_identity(
        [
            {"name": "high-sensitivity C-reactive protein", "record": {"mesh": "D002097"}},
            {"name": "hs-CRP", "record": {"mesh": "D002097"}},
            {"name": "inflammation", "record": {"mesh": "D007249"}},
        ],
        [{"names": ["high-sensitivity C-reactive protein", "hs-CRP"], "display": "C-reactive protein", "role": "target"}],
    )
    assert [end["name"] for end in kept_target] == ["inflammation"]
    assert target_report["applied"][0]["display"] == "C-reactive protein"
    assert "C-reactive protein" not in [end["name"] for end in kept_target]
    kept_label, label_report = apply_identity(
        [
            {"name": "high-sensitivity C-reactive protein", "record": {"mesh": "D002097"}},
            {"name": "hs-CRP", "record": {"mesh": "D002097"}},
        ],
        [{"names": ["high-sensitivity C-reactive protein", "hs-CRP"], "display": "C-reactive protein", "role": "node"}],
    )
    assert [end["name"] for end in kept_label] == ["high-sensitivity C-reactive protein", "hs-CRP"]
    assert label_report["skipped"][0]["reason"] == "display"
    kept_conflict, conflict_report = apply_identity(
        [
            {"name": "inflammation", "record": {"mesh": "D007249"}},
            {"name": "atherosclerotic plaque", "record": {"mesh": "D058226"}},
        ],
        [{"names": ["inflammation", "atherosclerotic plaque"], "display": "inflammation", "role": "node"}],
    )
    assert [end["name"] for end in kept_conflict] == ["inflammation", "atherosclerotic plaque"]
    assert conflict_report["conflicts"]
    kept_empty, empty_report = apply_identity(
        [
            {"name": "hs-CRP", "record": {"mesh": "D002097"}},
            {"name": "CRP", "record": {}},
        ],
        [{"names": ["hs-CRP", "CRP"], "display": "hs-CRP", "role": "target"}],
    )
    assert kept_empty == []
    assert empty_report["conflicts"] == []
    same_blank, same_blank_report = apply_identity(
        [
            {"name": "endothelial dysfunction", "record": {}},
            {"name": "endothelial function impairment", "record": {}},
        ],
        [{"names": ["endothelial dysfunction", "endothelial function impairment"], "display": "endothelial dysfunction", "role": "node"}],
    )
    assert [end["name"] for end in same_blank] == ["endothelial dysfunction"]
    assert "endothelial function impairment" in same_blank[0]["forms"]
    assert same_blank_report["conflicts"] == []
    different_blank, different_blank_report = apply_identity(
        [
            {"name": "endothelial dysfunction", "record": {}},
            {"name": "plaque rupture", "record": {}},
        ],
        [
            {"names": ["endothelial dysfunction"], "display": "endothelial dysfunction", "role": "node"},
            {"names": ["plaque rupture"], "display": "plaque rupture", "role": "node"},
        ],
    )
    assert [end["name"] for end in different_blank] == ["endothelial dysfunction", "plaque rupture"]
    assert different_blank_report["conflicts"] == []
    kept_join, _join_report = apply_identity(
        [{"name": "hs-CRP", "record": {"mesh": "D002097"}}],
        [{"names": ["hs-CRP"], "display": "high-sensitivity C-reactive protein", "role": "node"}],
        locked=[{"name": "high-sensitivity C-reactive protein", "forms": ["high-sensitivity C-reactive protein"], "record": {"mesh": "D002097"}}],
    )
    assert [end["name"] for end in kept_join] == ["high-sensitivity C-reactive protein"]
    assert "hs-CRP" in kept_join[0]["forms"]
    seen_queries = []

    def mesh_search(term: str) -> dict:
        seen_queries.append(term)
        return {"term": term, "count": "0", "records": []}

    def uniprot_search(query: str) -> dict:
        return {"query": query, "returned": 0, "rows": []}

    def ask_identity(messages: list[dict]) -> str:
        if len(messages) == 1:
            assert "CRP" in messages[0]["content"]
            assert "首先" not in messages[0]["content"]
            assert "names 里只写待判断名字的序号" in messages[0]["content"]
            return "MESH C-reactive protein[MeSH Terms]"
        assert '"count": "0"' in messages[-1]["content"]
        return 'IDENTITY\n{"groups":[{"names":[1],"display":1,"role":"target"}]}'

    judged, judged_report = judge_identity(
        ask_identity,
        [{"name": "hs-CRP", "record": {"mesh": "D002097"}}, {"name": "inflammation", "record": {"mesh": "D007249"}}],
        target="CRP",
        disease="Coronary Artery Disease",
        mesh="D003324",
        mesh_search=mesh_search,
        uniprot_search=uniprot_search,
    )
    assert seen_queries == ["C-reactive protein[MeSH Terms]"]
    assert [end["name"] for end in judged] == ["inflammation"]
    assert judged_report["applied"][0]["records"][0]["mesh"] == "D002097"

    def ask_display_index(messages: list[dict]) -> str:
        assert "1. endothelial dysfunction" in messages[0]["content"]
        assert "2. endothelial function impairment" in messages[0]["content"]
        return 'IDENTITY\n{"groups":[{"names":[1,2],"display":1,"role":"node"}]}'

    indexed, _indexed_report = judge_identity(
        ask_display_index,
        [
            {"name": "endothelial dysfunction", "record": {}},
            {"name": "endothelial function impairment", "record": {}},
        ],
        target="T",
        disease="D",
        mesh="",
        mesh_search=mesh_search,
        uniprot_search=uniprot_search,
    )
    assert [end["name"] for end in indexed] == ["endothelial dysfunction"]
    assert "endothelial function impairment" in indexed[0]["forms"]

    def ask_display_text(messages: list[dict]) -> str:
        return 'IDENTITY\n{"groups":[{"names":[1,2],"display":"endothelial dysfunction","role":"node"}]}'

    texted, text_report = judge_identity(
        ask_display_text,
        [
            {"name": "endothelial dysfunction", "record": {}},
            {"name": "endothelial function impairment", "record": {}},
        ],
        target="T",
        disease="D",
        mesh="",
        mesh_search=mesh_search,
        uniprot_search=uniprot_search,
    )
    assert [end["name"] for end in texted] == ["endothelial dysfunction", "endothelial function impairment"]
    assert text_report["skipped"][0]["reason"] == "display"

    def ask_name_text(messages: list[dict]) -> str:
        return 'IDENTITY\n{"groups":[{"names":["endothelial dysfunction","endothelial function impairment"],"display":1,"role":"node"}]}'

    named_text, named_report = judge_identity(
        ask_name_text,
        [
            {"name": "endothelial dysfunction", "record": {}},
            {"name": "endothelial function impairment", "record": {}},
        ],
        target="T",
        disease="D",
        mesh="",
        mesh_search=mesh_search,
        uniprot_search=uniprot_search,
    )
    assert [end["name"] for end in named_text] == ["endothelial dysfunction", "endothelial function impairment"]
    assert named_report["applied"] == []
    assert named_report["unjudged"] == ["endothelial dysfunction", "endothelial function impairment"]

    def ask_locked_index(messages: list[dict]) -> str:
        assert "2. high-sensitivity C-reactive protein" in messages[0]["content"]
        return 'IDENTITY\n{"groups":[{"names":[1],"display":2,"role":"node"}]}'

    joined_index, _joined_index_report = judge_identity(
        ask_locked_index,
        [{"name": "hs-CRP", "record": {"mesh": "D002097"}}],
        target="CRP",
        disease="D",
        mesh="",
        mesh_search=mesh_search,
        uniprot_search=uniprot_search,
        locked=[{"name": "high-sensitivity C-reactive protein", "forms": ["high-sensitivity C-reactive protein"], "record": {"mesh": "D002097"}}],
    )
    assert [end["name"] for end in joined_index] == ["high-sensitivity C-reactive protein"]
    assert "hs-CRP" in joined_index[0]["forms"]
    assert "C-Reactive Protein" not in TARGET_RECORD_TASK
    sent_queries = []

    def mesh_record(term: str) -> dict:
        sent_queries.append(term)
        if term == "C-Reactive Protein[MeSH Terms]":
            return {"records": [{"mesh": "D002097"}]}
        return {"records": []}

    def ask_record(messages: list[dict]) -> str:
        if len(messages) == 1:
            assert messages[0]["content"].startswith("靶点字符串：CRP")
            return "MESH C-Reactive Protein[MeSH Terms]"
        return 'RECORD\n{"mesh":"D002097","uniprot":"P02741","names":["C-reactive protein"]}'

    recorded = ask_target_ids(ask_record, "CRP", mesh_record, lambda query: {"rows": []})
    assert sent_queries == ["C-Reactive Protein[MeSH Terms]"]
    assert recorded["mesh"] == "D002097"
    assert recorded["uniprot"] == ""
    assert recorded["rejected_uniprot"] == "P02741"
    assert recorded["returned"] == []
    invented = ask_target_ids(
        lambda messages: 'RECORD\n{"mesh":"D002097","uniprot":"","names":[]}',
        "CRP",
        lambda term: {"records": []},
        lambda query: {"rows": []},
    )
    assert invented["mesh"] == ""
    assert invented["rejected_mesh"] == "D002097"
    untouched, missing_report = judge_identity(
        lambda messages: "not a judgment",
        [{"name": "inflammation", "record": {"mesh": "D007249"}}],
        target="CRP",
        disease="Coronary Artery Disease",
        mesh="D003324",
        mesh_search=mesh_search,
        uniprot_search=uniprot_search,
    )
    assert [end["name"] for end in untouched] == ["inflammation"]
    assert missing_report["missing"] is True
    assert _in_shown("inflammation", [{"role": "user", "content": "Pathology nodes already read:\ninflammation"}])
    assert proposal_names("mast cells → IL-13\nSTAT6", [], []) == ["mast cells → IL-13", "STAT6"]
    assert split_proposals("mast cells → IL-13\nSTAT6", [], [])[1] == []

    class Counting:
        def __init__(self):
            self.queries = []

        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            self.queries.append(query)
            names = re.findall(r'"([^"]+)"', query)
            text = f"{names[0]} links {names[1]}." if len(names) >= 2 else "empty"
            return [Paper("9", "t", text, None, "pubmed", "", "9", "")]

    def ask_arrow(messages: list[dict]) -> str:
        if "The whole reply is one line" in messages[-1]["content"]:
            return "related"
        if "Propose" in messages[-1]["content"]:
            return "mast cells → IL-13\nmesangial deposition"
        return "noted"

    counted = Counting()
    arrow_paths, arrow_gaps = walk_axes(ask_arrow, counted, "TargetA", start, [pathology], "DiseaseB", None)
    assert any(any("→" in step["name"] for step in path) for path in arrow_paths)
    assert all(item.get("stop") != "not a node" for item in arrow_gaps)
    assert any("→" in query for query in counted.queries)
    snap_start = {**start, "evidence": [{"sentence": "hs-CRP was measured."}]}
    snap_end = {"name": "inflammation", "forms": ["inflammation"], "record": {"mesh": "D002097"}}

    def ask_snap(messages: list[dict]) -> str:
        if "The whole reply is one line" in messages[-1]["content"]:
            return "related"
        if "Propose" in messages[-1]["content"] and "Left: hs-CRP" not in messages[-1]["content"]:
            return "hs-CRP"
        return ""

    snap_paths, snap_gaps = walk_axes(
        ask_snap,
        Counting(),
        "TargetA",
        snap_start,
        [snap_end],
        "DiseaseB",
        None,
        concept_of=lambda name: {"mesh": "D002097"},
    )
    assert snap_paths == []
    assert snap_gaps[0]["nodes"][-1] == "hs-CRP"
    assert snap_gaps[0]["stop"] == "no proposal"
    blocked_paths, blocked_gaps = walk_axes(
        ask_snap,
        Counting(),
        "TargetA",
        snap_start,
        [snap_end],
        "DiseaseB",
        None,
        identity_block=["hs-CRP"],
    )
    assert blocked_paths == []
    assert blocked_gaps[0]["stop"] == "no proposal"
    assert with_filter(pair_query("CRP", "CAD"), "Clinical Trial[pt]").endswith("AND (Clinical Trial[pt])")
    script = {"n": 0}

    def ask_questions(prompt: str) -> str:
        script["n"] += 1
        return "QUESTION\ttarget\tactions of the target\nQUESTION\tdisease\tpathology"

    assert questions(ask_questions, "Kv1.3", "IgA nephropathy")[0] == "actions of the target"
    assert script["n"] == 1
    try:
        questions(
            lambda prompt: "QUESTION\ttarget\trole in IgA nephropathy\nQUESTION\tdisease\tpathology",
            "Kv1.3",
            "IgA nephropathy",
        )
    except RuntimeError as exc:
        assert "not readable" in str(exc)
    else:
        raise AssertionError("a target question naming the disease was kept")
    assert "another tissue" in status_prompt([], [])
    assert pair_query_forms(["IL-10", "interleukin-10"], ["Heart Failure"]) == (
        '("IL-10"[tiab] OR "interleukin-10"[tiab]) AND ("Heart Failure"[tiab])'
    )
    pool = [{"sentence": str(index)} for index in range(12)]
    assert [row["sentence"] for row in choose_evidence(None, "L", "R", pool)] == [
        str(index) for index in range(SENTENCE_CAP)
    ]
    assert "does not increase" in status_prompt([], [{"sentence": "does not increase", "relation": "denied"}])

    class AliasClients:
        def search_papers(self, query, *, as_of=None, source=None, keep=50, strict=False):
            return [Paper("4", "t", "KCNA3 increases mesangial deposition.", None, "pubmed", "", "4", "")]

    alias_prompts = []

    def ask_alias(prompt: str) -> str:
        alias_prompts.append(prompt)
        return "related"

    alias_rows = outer_sentences(
        AliasClients(), "Kv1.3", "mesangial deposition", None, None,
        ["Kv1.3", "KCNA3"], ["mesangial deposition"], ask_alias,
    )
    assert alias_rows and alias_rows[0]["relation"] == "related"
    alias_head = alias_prompts[0].split("Sentence:", 1)[0]
    assert "KCNA3" in alias_head and "any one of them is the left end" in alias_head

    label_prompts = []

    def ask_label(prompt: str) -> str:
        label_prompts.append(prompt)
        return "co-occurrence"

    labeled = {"hops": [{
        "left": "Kv1.3",
        "right": "mesangial deposition",
        "left_forms": ["Kv1.3", "KCNA3"],
        "right_forms": ["mesangial deposition"],
        "sentences": [
            {"sentence": "KCNA3 promotes mesangial IgA deposition."},
            {"sentence": "already labeled", "relation": "related"},
        ],
    }]}
    label_axes([labeled], ask_label)
    assert len(label_prompts) == 1
    label_head = label_prompts[0].split("Sentence:", 1)[0]
    assert "KCNA3" in label_head and "any one of them is the left end" in label_head
    assert labeled["hops"][0]["sentences"][0]["relation"] == "co-occurrence"
    assert labeled["hops"][0]["sentences"][1]["relation"] == "related"

    stored_axis = assemble_axis(
        "Kv1.3", "DiseaseB",
        [{"name": "IgA production", "forms": ["IgA production"], "sentence": {"sentence": "x"}}],
        [],
        ["Kv1.3", "KCNA3"],
    )
    assert stored_axis["hops"][0]["left_forms"] == ["Kv1.3", "KCNA3"]
    assert stored_axis["hops"][0]["right_forms"] == ["IgA production"]
    join_end = {
        "name": "mesangial deposition",
        "forms": ["mesangial deposition"],
        "sentence": {"sentence": "Mesangial deposition is a feature of IgA nephropathy."},
    }
    disease_list = ["Glomerulonephritis, IGA", "IGA Nephropathy", "IgAN"]
    stored_joins = disease_joins(
        [{"hops": [{"right": "mesangial deposition"}]}],
        "Glomerulonephritis, IGA",
        [join_end],
        disease_list,
    )
    assert stored_joins[0]["right_forms"] == disease_list
    assert stored_joins[0]["left_forms"] == ["mesangial deposition"]

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        saved_path = Path(tmp) / "target-writings.json"
        saved_path.write_text(json.dumps(["Kv1.3", "KCNA3"]), encoding="utf-8")

        def ask_saved(prompt: str) -> str:
            raise AssertionError("saved target writings were asked again")

        assert target_writings_for_run(saved_path, True, ask_saved, "Kv1.3", ["KCNA3"]) == ["Kv1.3", "KCNA3"]
        fresh_path = Path(tmp) / "fresh.json"

        def ask_fresh(prompt: str) -> str:
            return "NODE\nKv1.3\nNAME\n2\nABBR\nKCNA3\nSAME\nnone"

        assert target_writings_for_run(fresh_path, False, ask_fresh, "Kv1.3", ["Kv1.3", "KCNA3"]) == [
            "Kv1.3", "KCNA3",
        ]
        assert json.loads(fresh_path.read_text(encoding="utf-8")) == ["Kv1.3", "KCNA3"]
        assert target_writings_for_run(fresh_path, True, ask_saved, "Kv1.3", []) == ["Kv1.3", "KCNA3"]
    sample = relation_prompt(
        ["mesangial IgA deposition"],
        ["Glomerulonephritis, IGA", "IgA nephropathy"],
        "IgA nephropathy is characterized by mesangial IgA deposition.",
    )
    assert "related:" in sample and "promotes:" not in sample and "inhibits:" not in sample
    assert "does not cause" in sample and "hope" in sample
    assert "IgA nephropathy" in sample.split("Sentence:", 1)[0]
    print("self-check ok", flush=True)


def label_saved(out_dir: Path, ask=ask_model) -> None:
    """Write relation labels from sentences already stored in this result."""
    _load_dotenv()
    path = out_dir / "axes.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    axes = payload.get("axes") or []
    if not axes or not axes[0].get("hops"):
        print(f"skip {out_dir}", flush=True)
        return
    label_axes(axes, ask)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    target = axes[0]["hops"][0]["left"]
    disease = axes[0]["hops"][-1]["right"]
    note = payload.get("status") or ""
    (out_dir / "report.md").write_text(report_md(target, disease, axes, note), encoding="utf-8")
    (out_dir / "report.html").write_text(present_html(target, disease, axes, note), encoding="utf-8")
    print(f"labeled {out_dir}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="Kv1.3")
    parser.add_argument("--mesh", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--as-of", default="", help="YYYY-MM-DD. Empty uses the latest literature.")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--label", default="")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--from-direct", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return
    if args.label:
        label_saved(Path(args.label))
        return
    if not args.mesh or not args.out:
        raise SystemExit("need --mesh and --out")
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    run(Path(args.out), target=args.target, mesh=args.mesh, as_of=as_of, direct=args.direct, from_direct=args.from_direct)


if __name__ == "__main__":
    _self_check()
    main()
