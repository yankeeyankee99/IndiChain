"""Score a target–disease evidence chain.

Seven items, each 0 to 1, weight 1. The total is their sum (max 7).
specific: both ends are names, not a bare word.
paper_id: the hop names a paper.
id_text: that paper id has stored text.
sentence: the hop gives a sentence, not only a conclusion.
in_cited: that sentence is a continuous substring of the text stored for that
paper id. Library rows and retrieved abstracts both count.
joins: 1 when that sentence names both ends and a relation; 0.4 when the
paper and sentence are present but do not join the ends.
not_shortcut: 0 for a self-loop or a direct target-to-disease hop.

The displayed chain is the consensus: a hop enters when one run's sentence
joins its two ends, then duplicate hops are dropped. The score uses every hop
the runs wrote, including self-loops, direct jumps, and a missed disease end.
One ordinary-model answer is scored as written.
# ponytail: joins is lexical overlap, not a parsed relation. in_cited reads
# only text already stored under that paper id. A real sentence whose paper
# was not saved scores 0. Upgrade: open the paper outside this workspace.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PARTIAL = 0.4
ITEMS = ("specific", "paper_id", "id_text", "sentence", "in_cited", "joins", "not_shortcut")

_BARE = {
    "inflammation",
    "immune response",
    "immune responses",
    "activation",
    "proliferation",
    "inhibition",
    "disease",
    "fibrosis",
    "proteinuria",
}
_GENERIC = {"protein", "gene", "channel", "molecule", "the", "a", "an"}
_MECH = re.compile(
    r"\b(caus|lead|degrad|transport|cataly|convert|produc|reabsorb|facilitat|"
    r"activat|secret|promot|increas|decreas|inhibit|bind|govern|clear|oxid|"
    r"block|suppress|releas|switch|deposit|carr|mediat|regulat|reduc|impair|"
    r"elevat|lower|form|accumul|result|link|associat|excess|mean)\w*\b",
    re.I,
)
_ALIASES = {
    "cystic fibrosis": ("cystic fibrosis", "cf"),
    "hypercholesterolemia": ("hypercholesterolemia", "familial hypercholesterolemia"),
    "diabetes mellitus": ("diabetes mellitus", "diabetes"),
    "hyperuricemia": ("hyperuricemia", "hua"),
    "glomerulonephritis iga": ("glomerulonephritis iga", "iga nephropathy", "igan"),
}


def norm(text: str) -> str:
    text = (text or "").casefold().replace("’", "'")
    for src, dst in (("γ", " gamma "), ("α", " alpha "), ("β", " beta "), ("μ", " u ")):
        text = text.replace(src, dst)
    text = text.replace("–", " ").replace("—", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def search_names(name: str) -> list[str]:
    found = list(phrases(name))
    key = norm(re.sub(r"\([^)]*\)", " ", name or ""))
    for aliases in _ALIASES.values():
        if key in aliases or any(phrase in aliases for phrase in found):
            found.extend(aliases)
    for short, long in _ACRONYM.items():
        if short in found or long in found:
            found.extend((short, long))
    return list(dict.fromkeys(found))


def phrases(name: str) -> list[str]:
    extras = re.findall(r"\(([^)]*)\)", name or "")
    base = re.sub(r"\([^)]*\)", " ", name or "")
    out = []
    for piece in [base, *extras]:
        piece = norm(piece)
        if piece and piece not in {"bad", "good"}:
            out.append(piece)
    return out


_ACRONYM = {
    "ldlr": "ldl receptor",
    "xo": "xanthine oxidase",
    "xdh": "xanthine dehydrogenase",
    "cftr": "cystic fibrosis transmembrane conductance regulator",
}


def names_match(left: str, right: str) -> bool:
    if same_entity(left, right):
        return True
    return bool(set(search_names(left)) & set(search_names(right)))


def same_entity(left: str, right: str) -> bool:
    a = phrases(left)
    b = phrases(right)
    if not a or not b:
        return False
    if set(a) & set(b):
        return True
    for x in a:
        for y in b:
            if x == y:
                return True
            short, long = (x, y) if len(x) <= len(y) else (y, x)
            if long.startswith(short + " ") and set(long.split()) - set(short.split()) <= _GENERIC:
                return True
    return False


def specific(name: str) -> bool:
    text = norm(re.sub(r"\([^)]*\)", " ", name or ""))
    return bool(text) and text not in _BARE


def _hits(hay: str, name: str) -> bool:
    for phrase in search_names(name):
        if re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", hay):
            return True
        tokens = [tok for tok in phrase.split() if len(tok) >= 3 or tok.isupper()]
        if tokens and all(re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", hay) for tok in tokens):
            return True
    return False


def _nested(left: str, right: str) -> str | None:
    """Return the longer phrase when one end's phrase contains the other."""
    longer = None
    for x in phrases(left):
        for y in phrases(right):
            if x != y and (x in y or y in x):
                longer = y if x in y else x
    return longer


def states(left: str, right: str, quote: str) -> bool:
    hay = norm(quote)
    if not hay or not _hits(hay, left) or not _hits(hay, right):
        return False
    if not _MECH.search(hay):
        return False
    longer = _nested(left, right)
    if not longer:
        return True
    masked = hay.replace(longer, " ")
    short_is_left = any(phrase != longer and phrase in longer for phrase in phrases(left))
    return _hits(masked, left if short_is_left else right)


def quote_in_text(quote: str, text: str) -> bool:
    q = (quote or "").strip().casefold()
    return bool(q) and q in (text or "").casefold()


def _blank_items() -> dict[str, float]:
    return {name: 0.0 for name in ITEMS}


def hop_items(
    left: str,
    right: str,
    quote: str,
    paper_id: str,
    target: str,
    disease: str,
    papers: dict[str, str] | None = None,
) -> dict[str, float]:
    pid = str(paper_id or "").strip()
    text = (quote or "").strip()
    has_id = 1.0 if pid else 0.0
    has_sentence = 1.0 if text else 0.0
    if states(left, right, quote):
        joins = 1.0
    elif has_id and has_sentence:
        joins = PARTIAL
    else:
        joins = 0.0
    shortcut = same_entity(left, right) or (same_entity(left, target) and same_entity(right, disease))
    stored = (papers or {}).get(pid, "")
    return {
        "specific": 1.0 if specific(left) and specific(right) else 0.0,
        "paper_id": has_id,
        "id_text": 1.0 if has_id and bool(stored.strip()) else 0.0,
        "sentence": has_sentence,
        "in_cited": 1.0 if has_sentence and has_id and quote_in_text(text, stored) else 0.0,
        "joins": joins,
        "not_shortcut": 0.0 if shortcut else 1.0,
    }


def hop_score(left: str, right: str, quote: str, paper_id: str, target: str, disease: str) -> float:
    if not specific(left) or not specific(right):
        return 0.0
    if same_entity(left, right):
        return 0.0
    if same_entity(left, target) and same_entity(right, disease):
        return 0.0
    if states(left, right, quote):
        return 1.0
    if (quote or "").strip() and str(paper_id or "").strip():
        return PARTIAL
    return 0.0


def split_step(step: str) -> tuple[str, str] | None:
    parts = re.split(r"\s*(?:\u2192|->)\s*", step or "")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0].strip(), parts[1].strip()


def _add_paper(papers: dict[str, str], paper_id: str, text: str) -> None:
    pid = str(paper_id or "").strip()
    body = str(text or "").strip()
    if pid and body:
        papers[pid] = papers.get(pid, "") + "\n" + body


def _library_rows(prompt: str) -> dict[str, str]:
    idx = prompt.find("Target library:")
    if idx < 0:
        return {}
    rows = {}
    for line in prompt[idx:].splitlines():
        head, sep, text = line.partition("\t")
        head = head.strip()
        if sep and len(head) >= 2 and head[0] in "TD" and head[1:].isdigit():
            rows[head] = text
    return rows


def _extract_blocks(prompt: str, papers: dict[str, str]) -> None:
    idx = prompt.find("\n[")
    if idx < 0:
        return
    for part in prompt[idx:].split("\n["):
        part = part.strip()
        if not part:
            continue
        pid, _, text = part.partition("]")
        _add_paper(papers, pid.lstrip("["), text)


def papers_by_id(flow_root: Path, library_root: Path | None = None) -> dict[str, str]:
    papers: dict[str, str] = {}
    if library_root and library_root.is_dir():
        for path in library_root.rglob("*library.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("lines") or data.get("rows") or [] if isinstance(data, dict) else data
            for row in rows:
                if isinstance(row, dict):
                    _add_paper(papers, str(row.get("paper_id") or ""), str(row.get("line") or row.get("quote") or ""))
    if not flow_root.is_dir():
        return papers
    for path in flow_root.rglob("chain.json"):
        chain = json.loads(path.read_text(encoding="utf-8"))
        listed: dict[str, str] = {}
        for item in chain.get("audit") or []:
            task = str(item.get("task") or "")
            prompt = str(item.get("prompt") or "")
            if task == "TASK extract":
                _extract_blocks(prompt, papers)
            elif task == "TASK ends":
                listed.update(_library_rows(prompt))
        for step in chain.get("steps") or []:
            key = f"{step.get('side') or ''}{step.get('n') or ''}"
            if key in listed:
                _add_paper(papers, str(step.get("paper_id") or ""), listed[key])
    return papers


def score_chain(chain: dict, papers: dict[str, str] | None = None) -> dict:
    steps = chain.get("steps") or []
    target = str(chain.get("target") or "")
    disease = str(chain.get("name") or chain.get("disease") or "")
    if not target and steps:
        first = split_step(str(steps[0].get("step") or ""))
        target = first[0] if first else ""
    hops = []
    for step in steps:
        ends = split_step(str(step.get("step") or ""))
        if not ends:
            hops.append({"step": step.get("step"), "score": 0.0, "items": _blank_items(), "counted": True})
            continue
        quote = " ".join(step.get("quotes") or [])
        items = hop_items(*ends, quote, str(step.get("paper_id") or ""), target, disease, papers)
        hops.append({
            "step": step.get("step"),
            "score": round(sum(items.values()), 3),
            "items": items,
            "counted": True,
        })
    counted = [item for item in hops if item["counted"]]
    if chain.get("reached") is False:
        blank = {"step": "disease end missing", "score": 0.0, "items": _blank_items(), "counted": True}
        counted.append(blank)
        hops.append(blank)
    means = {}
    for name in ITEMS:
        means[name] = round(sum(item["items"][name] for item in counted) / len(counted), 3) if counted else 0.0
    return {
        "score": round(sum(means.values()), 3),
        "items": means,
        "hops": hops,
        "reached": chain.get("reached"),
        "stop": chain.get("stop") or "",
    }


def score_runs(runs: list[dict], papers: dict[str, str] | None = None) -> dict:
    """Pool every hop the runs wrote. Each written step counts once per time it appears."""
    hops = []
    for chain in runs:
        hops.extend(score_chain(chain, papers)["hops"])
    counted = [hop for hop in hops if hop.get("counted")]
    means = {}
    for name in ITEMS:
        means[name] = round(sum(hop["items"][name] for hop in counted) / len(counted), 3) if counted else 0.0
    return {"score": round(sum(means.values()), 3), "items": means, "hops": hops}


def consensus_chain(runs: list[dict]) -> dict:
    """One mechanism from the runs. A hop enters only with a joining sentence."""
    target = ""
    disease = ""
    reached = False
    for chain in runs:
        target = target or str(chain.get("target") or "")
        disease = disease or str(chain.get("name") or chain.get("disease") or "")
        reached = reached or bool(chain.get("reached"))
        if not target and chain.get("steps"):
            first = split_step(str(chain["steps"][0].get("step") or ""))
            target = first[0] if first else ""
    kept: list[dict] = []
    seen: list[tuple[str, str]] = []
    for chain in runs:
        for step in chain.get("steps") or []:
            ends = split_step(str(step.get("step") or ""))
            if not ends:
                continue
            left, right = ends
            if names_match(left, right):
                continue
            if names_match(left, target) and names_match(right, disease):
                continue
            quote = " ".join(step.get("quotes") or [])
            if not states(left, right, quote):
                continue
            if any(
                (names_match(left, prev[0]) and names_match(right, prev[1]))
                or (names_match(left, prev[1]) and names_match(right, prev[0]))
                for prev in seen
            ):
                continue
            seen.append((left, right))
            kept.append(step)
    return {"target": target, "name": disease, "reached": reached, "steps": kept}


def _runs(pair_dir: Path) -> list[dict]:
    runs = []
    for path in sorted(pair_dir.rglob("chain.json")):
        chain = json.loads(path.read_text(encoding="utf-8"))
        chain["_file"] = str(path)
        runs.append(chain)
    return runs


def main() -> None:
    here = Path(__file__).resolve().parent
    flow_root = here / "chains" / "flow"
    grok_root = here / "chains" / "grok"
    library_root = here.parent / "完整流程 - 评分测试" / "round4(节点test)"
    papers = papers_by_id(flow_root, library_root)
    pairs = sorted(p.name for p in flow_root.iterdir() if p.is_dir())
    report = {"items": list(ITEMS), "weight": 1, "partial_join": PARTIAL, "max": len(ITEMS), "pairs": {}}
    print(f"{'pair':<8} {'flow':>6} {'grok':>6}  gap   hops")
    for pair in pairs:
        runs = _runs(flow_root / pair)
        agreed = consensus_chain(runs)
        flow_scored = score_runs(runs, papers)
        grok_runs = _runs(grok_root / pair) if (grok_root / pair).is_dir() else []
        grok_scored = score_chain(grok_runs[0], papers) if grok_runs else {"score": 0.0, "items": _blank_items(), "hops": []}
        gap = round(flow_scored["score"] - grok_scored["score"], 3)
        consensus = [step.get("step") for step in agreed.get("steps") or []]
        report["pairs"][pair] = {
            "flow": flow_scored["score"],
            "grok": grok_scored["score"],
            "flow_minus_grok": gap,
            "flow_items": flow_scored["items"],
            "grok_items": grok_scored["items"],
            "consensus": consensus,
            "written_hops": len(flow_scored["hops"]),
            "flow_hops": flow_scored["hops"],
            "grok_hops": grok_scored["hops"],
        }
        print(f"{pair:<8} {flow_scored['score']:6.3f} {grok_scored['score']:6.3f}  {gap:+.3f}  written {len(flow_scored['hops'])} consensus {len(consensus)}")
        print("  flow", " ".join(f"{name}={flow_scored['items'][name]:.3f}" for name in ITEMS))
        print("  grok", " ".join(f"{name}={grok_scored['items'][name]:.3f}" for name in ITEMS))
    dest = here / "scores.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(dest)


if __name__ == "__main__":
    main()
