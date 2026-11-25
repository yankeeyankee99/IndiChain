"""Summarize five finished chains before scoring.

The model merges names such as LDLR and LDL receptor. This file only packs
the chains into the prompt and rejects a reply that quotes a sentence the
runs did not write.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PROMPT = Path(__file__).with_name("summarize_prompt.md")
_SPLIT = re.compile(r"\s*(?:\u2192|->)\s*")


def _hops(chain: dict) -> list[dict]:
    hops = []
    for step in chain.get("steps") or []:
        parts = _SPLIT.split(str(step.get("step") or ""))
        if len(parts) != 2:
            continue
        quote = " ".join(step.get("quotes") or []).strip()
        hops.append({
            "left": parts[0].strip(),
            "right": parts[1].strip(),
            "paper_id": str(step.get("paper_id") or "").strip(),
            "quote": quote,
        })
    return hops


def load_runs(pair_dir: Path) -> list[dict]:
    runs = []
    for path in sorted(pair_dir.rglob("chain.json")):
        chain = json.loads(path.read_text(encoding="utf-8"))
        runs.append({
            "target": str(chain.get("target") or ""),
            "disease": str(chain.get("name") or chain.get("disease") or ""),
            "hops": _hops(chain),
        })
    return runs


def render_chains(runs: list[dict]) -> str:
    blocks = []
    for index, run in enumerate(runs, start=1):
        lines = [f"Run {index}"]
        for hop in run["hops"]:
            lines.append("\t".join((hop["left"], hop["right"], hop["paper_id"], hop["quote"])))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_prompt(target: str, disease: str, runs: list[dict]) -> str:
    text = _PROMPT.read_text(encoding="utf-8")
    return (
        text.replace("{{TARGET}}", target)
        .replace("{{DISEASE}}", disease)
        .replace("{{CHAINS}}", render_chains(runs))
    )


def _section_rows(reply: str, heading: str) -> list[list[str]]:
    rows = []
    seen = False
    for line in (reply or "").splitlines():
        head = line.strip()
        if head in {"NAMES", "AXIS", "GRAPH"}:
            seen = head == heading
            continue
        if not seen or not head:
            continue
        rows.append(line.split("\t"))
    return rows


def known_quotes(runs: list[dict]) -> set[str]:
    return {" ".join(hop["quote"].split()).casefold() for run in runs for hop in run["hops"] if hop["quote"]}


def foreign_sentences(reply: str, runs: list[dict]) -> list[str]:
    known = known_quotes(runs)
    found = []
    for heading in ("AXIS", "GRAPH"):
        for row in _section_rows(reply, heading):
            if len(row) < 5:
                continue
            quote = " ".join(row[4].split()).casefold()
            if quote and quote not in known:
                found.append(row[4].strip())
    return found


def graph_steps(reply: str) -> list[dict]:
    steps = []
    for row in _section_rows(reply, "GRAPH"):
        if len(row) < 5:
            continue
        left, right, _runs, paper_id, quote = (part.strip() for part in row[:5])
        if not left or not right:
            continue
        steps.append({"step": f"{left} → {right}", "quotes": [quote], "paper_id": paper_id})
    return steps


def accepted_graph(reply: str, runs: list[dict]) -> list[dict]:
    known = known_quotes(runs)
    kept = []
    for step in graph_steps(reply):
        quote = " ".join(step["quotes"][0].split()).casefold()
        if quote in known:
            kept.append(step)
    return kept


def _self_check() -> None:
    runs = [{
        "target": "PCSK9",
        "disease": "Hypercholesterolemia",
        "hops": [{
            "left": "PCSK9",
            "right": "LDLR",
            "paper_id": "1",
            "quote": "PCSK9 binds the LDL receptor.",
        }],
    }]
    prompt = build_prompt("PCSK9", "Hypercholesterolemia", runs)
    assert "PCSK9 binds the LDL receptor." in prompt
    assert "LDL receptor、LDLR" in prompt
    copied = "GRAPH\nPCSK9\tLDL receptor\tRun 1\t1\tPCSK9 binds the LDL receptor.\n"
    invented = "GRAPH\nPCSK9\tLDL receptor\tRun 1\t1\tPCSK9 invents a new sentence.\n"
    assert foreign_sentences(copied, runs) == []
    assert foreign_sentences(invented, runs) == ["PCSK9 invents a new sentence."]
    assert accepted_graph(copied, runs)[0]["step"] == "PCSK9 → LDL receptor"
    assert accepted_graph(invented, runs) == []


if __name__ == "__main__":
    import sys

    _self_check()
    if len(sys.argv) == 1:
        print("summary prompt ok")
    else:
        from run import _deepseek, _load_dotenv

        _load_dotenv()
        root = Path(sys.argv[1])
        only = sys.argv[2] if len(sys.argv) > 2 else ""
        for pair_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            if only and pair_dir.name != only:
                continue
            runs = load_runs(pair_dir)
            if not runs:
                continue
            target = next((run["target"] for run in runs if run["target"]), "")
            disease = next((run["disease"] for run in runs if run["disease"]), "")
            if not target and runs[0]["hops"]:
                target = runs[0]["hops"][0]["left"]
            prompt = build_prompt(target, disease, runs)
            reply = _deepseek(prompt, timeout=600)
            (pair_dir / "summary.txt").write_text(reply, encoding="utf-8")
            steps = accepted_graph(reply, runs)
            graph = {"target": target, "name": disease, "steps": steps}
            (pair_dir / "graph.json").write_text(
                json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            dropped = len(graph_steps(reply)) - len(steps)
            print(f"{pair_dir.name} graph {len(steps)} dropped {dropped}", flush=True)
