"""Target-side reading screen over stored papers. Resumable. Does not write sqlite."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import _deepseek, _load_dotenv, load_scope, reading_ask, reading_question, reading_screen, target_named

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "run.sqlite"
OUT = ROOT / "data" / "target_effect_lines.json"
BATCH = 8
# Same wait as the chain ask. A batch still running after this is left undone.
SCREEN_TIMEOUT = 900
REASON_NO_COPY = "no sentence was copied for this paper"
REASON_NOT_SPAN = "copied text is not an exact abstract sentence that names the target"
REASON_SECOND = "second reading did not list this sentence under Sentences kept"
REASON_NO_NAME = "sentence does not name the target"
REASON_EMPTY_SLOT = "cell, process, and molecule are empty"


def _scope_file() -> Path:
    if "--scope" in sys.argv:
        return Path(sys.argv[sys.argv.index("--scope") + 1])
    return ROOT / "scope.yaml"


def _names_from_query(query: str) -> list[str]:
    left = query.split(" AND ", 1)[0].strip()
    if left.startswith("(") and left.endswith(")"):
        left = left[1:-1]
    return [p.strip() for p in left.split(" OR ") if p.strip()]


def _load_papers(store_path: Path = STORE) -> tuple[list[dict], str]:
    row = sqlite3.connect(store_path).execute(
        "SELECT payload FROM records WHERE kind = ?", ("papers",)
    ).fetchone()
    if not row:
        return [], ""
    payload = json.loads(row[0])
    papers = []
    for row in payload.get("papers") or []:
        papers.append(
            {
                "paper_id": str(row.get("paper_id") or ""),
                "title": str(row.get("title") or ""),
                "text": str(row.get("text") or ""),
            }
        )
    return papers, str(payload.get("query") or "")


def _body(paper: dict) -> str:
    return str(paper.get("text") or "")


def _exact_span(needle: str, haystack: str) -> str | None:
    n = (needle or "").strip()
    if not n:
        return None
    i = haystack.casefold().find(n.casefold())
    if i < 0:
        return None
    return haystack[i : i + len(n)]


def _strip_noise(line: str) -> str:
    line = line.strip().strip('"').strip("'")
    line = re.sub(r"^[\-\*]+\s*", "", line)
    line = re.sub(r"^\d+[\.\)]\s+", "", line)
    line = re.sub(r"^(?:kept|留下|保留|行)\s*[:：]\s*", "", line, flags=re.I)
    return line.strip()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _named_span(quote: str, paper: dict, names: list[str]) -> str | None:
    """Exact abstract span that contains quote and names the target."""
    body = _body(paper)
    span = _exact_span(quote, body)
    if not span:
        return None
    if target_named(span, names):
        return span
    for sent in _sentences(body):
        if span.casefold() not in sent.casefold():
            continue
        hit = _exact_span(sent, body)
        if hit and target_named(hit, names):
            return hit
    return None


def _slot_fields(line: str, batch: list[dict], quote: str = "") -> dict[str, str]:
    """Fields before the abstract quote are cell, process, and molecule."""
    parts = [p.strip().strip('"').strip("'") for p in _strip_noise(line).split("|")]
    parts = [p for p in parts if p]
    empty = {"cell": "", "process": "", "molecule": ""}
    if not parts:
        return empty
    if parts[0] in {p["paper_id"] for p in batch}:
        parts = parts[1:]
    quote_cf = quote.casefold()
    quote_at = None
    if quote_cf:
        for i, part in enumerate(parts):
            if quote_cf in part.casefold() or part.casefold() in quote_cf:
                quote_at = i
                break
    slots = parts[:quote_at] if quote_at is not None else []
    cell, process, molecule = (slots + ["", "", ""])[:3]
    return {"cell": cell, "process": process, "molecule": molecule}


def _candidate_spans(line: str, batch: list[dict], names: list[str]) -> list[tuple[str, str]]:
    """Return (paper_id, exact abstract span) pairs for one model line."""
    line = _strip_noise(line)
    if not line or line.upper() == "NONE":
        return []
    pid = ""
    rest = line
    m = re.match(r"^([A-Za-z0-9_.\-]+)\s*[\|:]\s*(.+)$", line)
    if m and any(m.group(1) == p["paper_id"] for p in batch):
        pid, rest = m.group(1), m.group(2).strip()
    papers = [p for p in batch if p["paper_id"] == pid] if pid else batch
    candidates = [rest]
    if "|" in rest:
        candidates = [
            part.strip().strip('"').strip("'")
            for part in rest.split("|")
            if part.strip()
        ]
        candidates.reverse()
    out = []
    for cand in candidates:
        for paper in papers:
            span = _named_span(cand, paper, names)
            if span:
                out.append((paper["paper_id"], span))
        if out:
            break
    return out


def _parse_lines(text: str) -> list[str]:
    rows = []
    for raw in (text or "").splitlines():
        line = _strip_noise(raw)
        if not line or line.upper() == "NONE":
            continue
        if line.startswith("#") or line.startswith("```"):
            continue
        rows.append(line)
    return rows


def _load_state(names: list[str], total: int, out_path: Path = OUT) -> dict:
    if out_path.exists():
        state = json.loads(out_path.read_text(encoding="utf-8"))
        state.setdefault("names", names)
        state.setdefault("papers_total", total)
        state.setdefault("papers_done", 0)
        state.setdefault("lines", [])
        state.setdefault("dropped", [])
        state.setdefault("errors", [])
        state["papers_with_lines"] = len({r["paper_id"] for r in state["lines"]})
        return state
    return {
        "names": names,
        "papers_total": total,
        "papers_done": 0,
        "lines": [],
        "papers_with_lines": 0,
        "dropped": [],
        "errors": [],
    }


def _save(state: dict, out_path: Path = OUT) -> None:
    state["papers_with_lines"] = len({r["paper_id"] for r in state["lines"]})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _done_ids(state: dict, _papers: list[dict]) -> set[str]:
    """Judged papers only. A timeout is not finished, and papers_done is not a watermark."""
    done = {r["paper_id"] for r in state.get("lines") or [] if r.get("paper_id")}
    done |= {r["paper_id"] for r in state.get("dropped") or [] if r.get("paper_id")}
    return done


def _kept_section(reply: str) -> str:
    text = reply or ""
    for marker in (
        "所保留的原句",
        "所保留的记录",
        "留下的行",
        "留下：",
        "留下的是",
        "Kept lines",
        "kept lines",
        "Kept:",
        "Sentences kept",
    ):
        i = text.find(marker)
        if i >= 0:
            text = text[i:]
            break
    for marker in ("\n丢弃", "\n其余行", "\n其余行排除", "\n予以排除", "\nDiscard", "\nDropped"):
        j = text.find(marker)
        if j >= 0:
            text = text[:j]
    return text


def _quota_stop(err: str) -> bool:
    text = (err or "").casefold()
    return "429" in text or "too many requests" in text


def _drop(paper_id: str, line: str, reason: str) -> dict:
    return {"paper_id": paper_id, "line": line, "reason": reason}


def _line_paper_id(line: str, batch: list[dict]) -> str:
    match = re.match(r"^([A-Za-z0-9_.\-]+)\s*[\|:]\s*", _strip_noise(line))
    if match and any(match.group(1) == paper["paper_id"] for paper in batch):
        return match.group(1)
    return ""


def _copy_reason(line: str, papers: list[dict]) -> str:
    rest = _strip_noise(line)
    match = re.match(r"^[A-Za-z0-9_.\-]+\s*[\|:]\s*(.+)$", rest)
    if match:
        rest = match.group(1).strip()
    parts = [part.strip() for part in rest.split("|") if part.strip()] or [rest]
    for paper in papers:
        body = _body(paper)
        for cand in parts:
            if _exact_span(cand, body):
                return REASON_NO_NAME
    return REASON_NOT_SPAN


def _resolve(
    batch: list[dict], names: list[str], reply1: str
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], dict[str, str]], list[dict]]:
    resolved: list[tuple[str, str]] = []
    seen_span: set[tuple[str, str]] = set()
    slots_for: dict[tuple[str, str], dict[str, str]] = {}
    matched: set[int] = set()
    tagged_miss: dict[str, list[str]] = {}
    step1 = _parse_lines(reply1)
    for idx, line in enumerate(step1):
        hits = _candidate_spans(line, batch, names)
        if hits:
            matched.add(idx)
            for pid, span in hits:
                key = (pid, span.casefold())
                if key in seen_span:
                    continue
                seen_span.add(key)
                resolved.append((pid, span))
                slots_for[key] = _slot_fields(line, batch, span)
            continue
        pid = _line_paper_id(line, batch)
        if pid:
            tagged_miss.setdefault(pid, []).append(line)
    have = {pid for pid, _span in resolved}
    dropped: list[dict] = []
    for paper in batch:
        pid = paper["paper_id"]
        if not pid:
            continue
        for text in tagged_miss.get(pid) or []:
            dropped.append(_drop(pid, text, _copy_reason(text, [paper])))
        if pid not in have and pid not in tagged_miss:
            dropped.append(_drop(pid, "", REASON_NO_COPY))
    for idx, line in enumerate(step1):
        if idx in matched or _line_paper_id(line, batch):
            continue
        dropped.append(_drop("", line, _copy_reason(line, batch)))
    return resolved, slots_for, dropped


def _apply_screen(
    batch: list[dict],
    names: list[str],
    resolved: list[tuple[str, str]],
    slots_for: dict[tuple[str, str], dict[str, str]],
    reply2: str,
) -> tuple[list[dict], list[dict]]:
    section = _kept_section(reply2)
    screened = section.casefold()
    listed: set[tuple[str, str]] = set()
    for line in _parse_lines(section):
        for pid, span in _candidate_spans(line, batch, names):
            listed.add((pid, span.casefold()))
    kept: list[dict] = []
    dropped: list[dict] = []
    kept_keys: set[tuple[str, str]] = set()
    titles = {p["paper_id"]: str(p.get("title") or "") for p in batch}
    for pid, span in resolved:
        key = (pid, span.casefold())
        row = f"{pid} | {span}".casefold()
        if key not in listed and span.casefold() not in screened and row not in screened:
            dropped.append(_drop(pid, span, REASON_SECOND))
            continue
        if not target_named(span, names):
            dropped.append(_drop(pid, span, REASON_NO_NAME))
            continue
        if key in kept_keys:
            continue
        slots = slots_for.get(key) or {"cell": "", "process": "", "molecule": ""}
        if not any(str(value).strip() for value in slots.values()):
            dropped.append(_drop(pid, span, REASON_EMPTY_SLOT))
            continue
        kept_keys.add(key)
        kept.append({
            "paper_id": pid,
            "title": titles.get(pid, ""),
            "line": span,
            "cell": slots["cell"],
            "process": slots["process"],
            "molecule": slots["molecule"],
        })
    return kept, dropped


def _process_batch(batch: list[dict], names: list[str], scope: dict) -> tuple[list[dict], list[dict]]:
    question = reading_question(scope["reading_question"], names)
    blocks = [
        f"ID: {p['paper_id']}\nABSTRACT: {(p['text'] or '').replace(chr(10), ' ')}"
        for p in batch
    ]
    # Packaging only: keep scope reading_question verbatim; ask for copied quotes so
    # the keep rule (exact abstract span) can run.
    ask = reading_ask(
        question,
        str(scope.get("reading_line") or "ID | cell | process | molecule | sentence"),
        "\n\n".join(blocks),
    )
    reply1 = _deepseek(ask, model=scope["model_copy"], timeout=SCREEN_TIMEOUT)
    resolved, slots_for, dropped = _resolve(batch, names, reply1)
    if not resolved:
        return [], dropped
    screen_prompt = reading_screen(
        scope["reading_screen"],
        [f"{pid} | {span}" for pid, span in resolved],
        names,
    )
    reply2 = _deepseek(screen_prompt, model=scope["model_copy"], timeout=SCREEN_TIMEOUT)
    kept, late = _apply_screen(batch, names, resolved, slots_for, reply2)
    return kept, dropped + late


def screen_papers(scope: dict, store_path: Path, out_path: Path) -> dict:
    papers, query = _load_papers(store_path)
    names = list(dict.fromkeys([*(scope.get("target_names") or []), *_names_from_query(query)]))
    state = _load_state(names, len(papers), out_path)
    state["names"] = names
    state["papers_total"] = len(papers)
    state.setdefault("dropped", [])
    # Stored errors are unfinished calls, not exclusions. The next pass submits them again.
    state["errors"] = []
    done = _done_ids(state, papers)
    state["papers_done"] = sum(1 for paper in papers if paper["paper_id"] in done)
    _save(state, out_path)
    todo = [p for p in papers if p["paper_id"] and p["paper_id"] not in done]
    print(f"papers {len(papers)} done {len(done)} todo {len(todo)}", flush=True)
    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        ids = [p["paper_id"] for p in batch]
        print(f"batch {ids[0]}..{ids[-1]} n={len(batch)}", flush=True)
        stop = False
        try:
            kept, dropped = _process_batch(batch, names, scope)
            state["lines"].extend(kept)
            state["dropped"].extend(dropped)
        except Exception as e:  # noqa: BLE001 — leave the batch undone for the next run
            err = str(e).strip().splitlines()[0][:200]
            if "ARK_API_KEY" in err:
                raise
            # ponytail: one pass per run. A timed-out batch stays out of lines and dropped, so the next run submits it again.
            # A 429 means the quota is spent; later batches in this pass would be refused too.
            print(f"error batch {err}", flush=True)
            stop = _quota_stop(err)
        done = _done_ids(state, papers)
        state["papers_done"] = sum(1 for paper in papers if paper["paper_id"] in done)
        _save(state, out_path)
        print(
            f"flush lines={len(state['lines'])} dropped={len(state['dropped'])} "
            f"done={state['papers_done']}",
            flush=True,
        )
        if stop:
            break
    state["papers_done"] = sum(1 for paper in papers if paper["paper_id"] in _done_ids(state, papers))
    _save(state, out_path)
    print(
        f"finished papers_done={state['papers_done']} "
        f"with_lines={state['papers_with_lines']} "
        f"lines={len(state['lines'])} dropped={len(state['dropped'])}",
        flush=True,
    )
    return state


def main() -> None:
    scope_path = _scope_file()
    if scope_path.resolve() != (ROOT / "scope.yaml").resolve():
        print("named-disease scope is not read by this Kv1.3 target-line file", flush=True)
        return
    _load_dotenv()
    screen_papers(load_scope(scope_path), STORE, OUT)


def _self_check() -> None:
    # ponytail: one assert-based check for span + resume skip
    assert _exact_span("Kv1.3 block", "The Kv1.3 block stops growth") == "Kv1.3 block"
    assert _names_from_query("(A OR B) AND (x OR y)") == ["A", "B"]
    paper = {
        "paper_id": "1",
        "title": "T",
        "text": (
            "Consistent with their channel phenotypes, proliferation of naive B cells "
            "is suppressed by TRAM-34 but not by the potent Kv1.3 blocker ShK, whereas "
            "the proliferation of class-switched memory B cells is suppressed by ShK "
            "but not TRAM-34."
        ),
    }
    names = ["Kv1.3", "KCNA3"]
    quote = "the proliferation of class-switched memory B cells is suppressed by ShK but not TRAM-34"
    span = _named_span(quote, paper, names)
    assert span and "Kv1.3" in span
    short = {"paper_id": "9", "title": "", "text": "PCSK9 binds LDLR."}
    hits = _candidate_spans(
        "9 | hepatocyte | lysosomal degradation of the LDL receptor protein | PCSK9 binds LDLR.",
        [short],
        ["PCSK9"],
    )
    assert hits == [("9", "PCSK9 binds LDLR.")]
    titled = {"paper_id": "3", "title": "Kv1.3 blockade in colitis", "text": "No channel is named here."}
    assert _named_span("Kv1.3 blockade in colitis", titled, ["Kv1.3"]) is None
    fake = {
        "names": [],
        "papers_total": 2,
        "papers_done": 1,
        "lines": [{"paper_id": "1", "line": "x"}],
        "errors": [],
    }
    papers = [{"paper_id": "1"}, {"paper_id": "2"}]
    assert "1" in _done_ids(fake, papers)
    fake["errors"] = [{"paper_id": "2", "error": "LLM wall-clock timeout after 180s"}]
    fake["papers_done"] = 2
    assert _done_ids(fake, papers) == {"1"}
    fake["dropped"] = [{"paper_id": "2", "line": "", "reason": REASON_NO_COPY}]
    assert "2" in _done_ids(fake, papers)
    assert _quota_stop("HTTP Error 429: Too Many Requests")
    assert not _quota_stop("LLM wall-clock timeout after 900s")
    one = {"paper_id": "9", "title": "T", "text": "PCSK9 binds LDLR. Rain falls."}
    names = ["PCSK9"]
    resolved, _slots, missed = _resolve([one], names, "NONE")
    assert resolved == [] and missed[0]["reason"] == REASON_NO_COPY
    resolved, _slots, missed = _resolve([one], names, "9 | PCSK9 flies away.")
    assert resolved == [] and missed[0]["reason"] == REASON_NOT_SPAN
    resolved, _slots, missed = _resolve([one], names, "9 | Rain falls.")
    assert resolved == [] and missed[0]["reason"] == REASON_NO_NAME
    resolved, slots, missed = _resolve(
        [one], names, "9 | hepatocyte | degradation | LDLR | PCSK9 binds LDLR."
    )
    assert resolved == [("9", "PCSK9 binds LDLR.")] and missed == []
    kept, late = _apply_screen(
        [one], names, resolved, slots, "Summary.\n\nSentences kept\n9 | PCSK9 binds LDLR."
    )
    assert kept and kept[0]["cell"] == "hepatocyte" and late == []
    kept, late = _apply_screen([one], names, resolved, slots, "Sentences kept\n")
    assert kept == [] and late[0]["reason"] == REASON_SECOND
    empty = {("9", "pcsk9 binds ldlr."): {"cell": "", "process": "", "molecule": ""}}
    kept, late = _apply_screen(
        [one], names, resolved, empty, "Sentences kept\n9 | PCSK9 binds LDLR."
    )
    assert kept == [] and late[0]["reason"] == REASON_EMPTY_SLOT
    print("self_check ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        _self_check()
    else:
        main()
