"""Sentence store and a parallel evidence net for one target and one disease.

The older walker in llm.py is not this run. PubMed only. DeepSeek-V4-Pro writes
queries and node names. The program checks sentences.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from threading import Semaphore
from urllib.error import HTTPError

from libraries import LiveClients
from llm import (
    NODE_RULE,
    disease_question,
    name_problem,
    parse_disease_role,
    quote_in_text,
)
from run import MODEL_PRO, _deepseek, _load_dotenv, load_scope, target_named

SIDE_CAP = 1000
KEEP_VERIFIED = 3
SHOW_ENDS = 10
EVIDENCE_SENTENCES = 10
HOP_FETCH = 100
READ_BATCH = 30
# ponytail: one arm stops after 6 hops so a wide target cannot walk forever.
# Upgrade: stop only when every verified child has been expanded.
DEPTH_CAP = 6
# ponytail: eight model calls at once. A 429 waits outside the slot, from 90s upward. Upgrade: raise if those waits stay rare.
MODEL_WORKERS = 8
model_slots = Semaphore(MODEL_WORKERS)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\ufeff\u2060]")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def plain_sentence(text: str) -> str:
    # ponytail: a hyphen before a newline stays, so "alp-" plus a newline plus "ha" becomes "alp- ha". Upgrade: a dictionary if those breaks must rejoin.
    return " ".join(_INVISIBLE.sub("", text).split())


def split_sentences(text: str) -> list[str]:
    out = []
    for part in _SENT_SPLIT.split(text or ""):
        plain = plain_sentence(part)
        if plain:
            out.append(plain)
    return out


def labeled_lines(reply: str, label: str) -> list[str]:
    found = []
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 2 or parts[0].upper() != label or not parts[1]:
            continue
        if parts[1] not in found:
            found.append(parts[1])
    return found


def keep_ranked(verified: list[str], ordered: list[str], limit: int = KEEP_VERIFIED) -> list[str]:
    """Keep the model's order. Names the model did not list are not padded in."""
    wanted = []
    for name in verified:
        if name not in wanted:
            wanted.append(name)
    if len(wanted) <= limit:
        return wanted
    out = []
    allowed = set(wanted)
    for name in ordered:
        if name in allowed and name not in out:
            out.append(name)
        if len(out) == limit:
            break
    return out


def ask_model(prompt: str) -> str:
    # IgAN sets NET_PRIORITY=1 so a shared rate limit yields to that run.
    priority = os.environ.get("NET_PRIORITY") == "1"
    delay = 20 if priority else 90
    for _ in range(12):
        try:
            with model_slots:
                return _deepseek(prompt, model=MODEL_PRO, timeout=900)
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


def _or_query(names: list[str]) -> str:
    parts = []
    for name in dict.fromkeys(n.strip() for n in names if n and n.strip()):
        parts.append(f'"{name}"[tiab]')
    return "(" + " OR ".join(parts) + ")"


def resolve_target(scope: dict, clients: LiveClients) -> dict:
    names = [str(x) for x in (scope.get("target_names") or []) if str(x).strip()]
    if scope.get("target"):
        names.insert(0, str(scope["target"]))
    last = ""
    for name in dict.fromkeys(names):
        try:
            hit = clients.uniprot(name)
        except RuntimeError as exc:
            last = str(exc)
            continue
        aliases = [str(x) for x in hit.get("aliases") or [] if str(x).strip()]
        folded = {item.casefold() for item in aliases}
        if name.casefold() not in folded and hit.get("uniprot_id", "").casefold() != name.casefold():
            continue
        return {
            "symbol": hit.get("name") or name,
            "uniprot_id": hit.get("uniprot_id") or "",
            "recommended": next((item for item in aliases if " " in item), hit.get("name") or name),
            "names": aliases,
        }
    raise RuntimeError(last or "no UniProt record matched a scope name")


def resolve_disease(scope: dict, clients: LiveClients) -> dict:
    mesh = str(scope.get("disease_mesh") or "").strip()
    written = str(scope.get("disease_name") or "").strip()
    preferred = clients.mesh_preferred_name(mesh) if mesh else written
    terms = clients.mesh_entry_terms(mesh) if mesh else []
    names = list(dict.fromkeys([preferred, *terms, written]))
    folded = {item.casefold() for item in names if item}
    if written and written.casefold() not in folded:
        raise RuntimeError(f"{written} is not a name on {mesh}")
    return {"mesh": mesh, "preferred": preferred or written, "names": [item for item in names if item]}


def paper_sentences(papers, names: list[str], side: str) -> list[dict]:
    rows = []
    seen = set()
    for paper in papers:
        for sentence in split_sentences(paper.text):
            if _CJK.search(sentence):
                continue
            if not target_named(sentence, names):
                continue
            key = (paper.paper_id, sentence.casefold())
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "doi": paper.doi or "",
                "pmid": paper.paper_id,
                "date": paper.published.isoformat() if paper.published else "",
                "title": paper.title,
                "sentence": sentence,
                "side": side,
            })
    return rows


def harvest(clients: LiveClients, query: str, as_of: date | None, keep: int) -> list:
    priority = os.environ.get("NET_PRIORITY") == "1"
    delay = 20 if priority else 90
    for _ in range(10):
        try:
            return clients.search_papers(query, as_of=as_of, source="pubmed", keep=keep, strict=True)
        except HTTPError as exc:
            if exc.code != 429:
                raise
            print(f"pubmed 429 pause {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 600 if priority else 1800)
    raise RuntimeError("PubMed rate limit")


def both_present(sentence: str, left: str, right: str) -> bool:
    """Each name is its own continuous span. Order in the sentence does not matter."""
    return target_named(sentence, [left]) and target_named(sentence, [right])


def pair_query_forms(left_forms: list[str], right_forms: list[str]) -> str:
    """One side is an OR of its registered writings. The two sides stay AND."""
    def side(forms: list[str]) -> str:
        parts = []
        seen = set()
        for name in forms:
            text = " ".join(str(name).replace('"', " ").split())
            if not text or text.casefold() in seen:
                continue
            seen.add(text.casefold())
            parts.append(f'"{text}"[tiab]')
        return "(" + " OR ".join(parts) + ")" if parts else '(""[tiab])'

    return f"{side(left_forms)} AND {side(right_forms)}"


def pair_query(left: str, right: str) -> str:
    return pair_query_forms([left], [right])


def hop_sentences(papers, left: str, right: str) -> list[dict]:
    """Papers are already in relevance order. Keep sentence order within a paper."""
    found = []
    for paper in papers:
        for sentence in split_sentences(paper.text):
            if _CJK.search(sentence) or not both_present(sentence, left, right):
                continue
            found.append({
                "doi": paper.doi or "",
                "pmid": paper.paper_id,
                "date": paper.published.isoformat() if paper.published else "",
                "title": paper.title,
                "sentence": sentence,
                "side": "gap",
            })
    return found


def take_evidence(rows: list[dict], limit: int = EVIDENCE_SENTENCES) -> list[dict]:
    return rows[:limit]


def library_hits(rows: list[dict], anchors: list[str], span: str) -> list[dict]:
    hits = []
    for row in rows:
        sentence = row["sentence"]
        if target_named(sentence, anchors) and target_named(sentence, [span]):
            hits.append(row)
        if len(hits) >= EVIDENCE_SENTENCES:
            break
    return hits


def read_nodes(ask, sentences: list[dict], anchor: str, kind: str) -> list[tuple[str, dict]]:
    """One pass over the library. A copied name must be a continuous span of that sentence."""
    found: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for start in range(0, len(sentences), READ_BATCH):
        chunk = sentences[start : start + READ_BATCH]
        body = "\n".join(f"S{i}\t{row['sentence']}" for i, row in enumerate(chunk, 1))
        reply = ask(
            "TASK read\n"
            f"Kind: {kind}\nAnchor: {anchor}\n"
            + NODE_RULE
            + "\nCopy a node only when those words are a continuous span of one sentence.\n"
            "Do not invent wording. One line: S number, tab, the exact words.\n\n"
            + body
        )
        for line in (reply or "").splitlines():
            parts = [part.strip() for part in line.split("\t")]
            if len(parts) < 2 or not parts[0].upper().startswith("S") or not parts[0][1:].isdigit():
                continue
            idx = int(parts[0][1:]) - 1
            if idx < 0 or idx >= len(chunk):
                continue
            span = parts[1]
            sentence = chunk[idx]["sentence"]
            if span.casefold() == anchor.casefold() or not target_named(sentence, [span]):
                continue
            key = span.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append((span, chunk[idx]))
        print(f"read {kind} {start}+{len(chunk)} nodes {len(found)}", flush=True)
    return found


def gap_search(clients, rows, left: str, right: str, as_of) -> list[dict]:
    papers = harvest(clients, pair_query(left, right), as_of, HOP_FETCH)
    added = hop_sentences(papers, left, right)
    rows.extend(added)
    return take_evidence(added)


def propose(ask, kind: str, left: str, disease: str, already: list[str], hops: list[dict] | None = None, pathology: list[str] | None = None) -> list[str]:
    body = (
        f"TASK nodes\nKind: {kind}\nLeft: {left}\nDisease: {disease}\n"
        + NODE_RULE
        + "\nA sentence about another disease may still supply the mechanism.\n"
        "A disease name is not a node.\n"
        "List every credible node of this kind that heads toward the disease.\n"
        "One line: NODE, tab, the name. Fewer is correct. Do not pad.\n"
    )
    if pathology:
        body += "Pathology already read from the disease library:\n" + "\n".join(pathology[:40]) + "\n"
    if hops:
        body += "Path so far:\n" + "\n".join(
            f"{hop['left']} -> {hop['right']}\n{hop['papers'][0]['sentence'][:240]}"
            for hop in hops if hop.get("papers")
        ) + "\n"
    if already:
        body += "Already tried from this left, with no sentence:\n" + "\n".join(already) + "\nDo not repeat these.\n"
    reply = ask(body)
    kept = []
    for name in labeled_lines(reply, "NODE"):
        if name_problem(name, left, "") in ("", "node_is_target_end"):
            if name not in kept and name not in already:
                kept.append(name)
    return kept


def disease_role(ask, name: str, disease: str) -> str:
    if name_problem(name, "", disease) == "node_is_target_end":
        return "end"
    reply = ask(disease_question(name, disease))
    return parse_disease_role(reply) or "no"


def rank_nodes(ask, nodes: list[tuple[str, dict]], disease: str) -> list[str]:
    if len(nodes) <= KEEP_VERIFIED:
        return [name for name, _row in nodes]
    lines = []
    for name, row in nodes:
        lines.append(f"{name}\t{row['sentence'][:240]}")
    reply = ask(
        "TASK rank\n"
        f"Disease: {disease}\n"
        "These nodes already have a sentence naming both ends.\n"
        "Order them by your confidence and by how directly the step moves toward the disease.\n"
        "Put the strongest first. One line: NODE, tab, the name.\n\n"
        + "\n".join(lines)
    )
    ordered = labeled_lines(reply, "NODE")
    kept = keep_ranked([name for name, _row in nodes], ordered)
    if kept:
        return kept
    # ponytail: a rank reply with no verified name would drop the arm. Keep the first three verified names.
    # Upgrade: stop the arm when the rank reply names none of them.
    print("rank empty, keep proposal order", flush=True)
    return [name for name, _row in nodes][:KEEP_VERIFIED]


def walk(
    out_dir: Path,
    *,
    target_name: str,
    disease_name: str,
    mesh: str,
    as_of: date | None,
) -> None:
    _load_dotenv()
    for key in ("Accesskey", "BOHR_ACCESS_KEY", "BOHRIUM_API_KEY"):
        os.environ.pop(key, None)
    scope = {
        "target": target_name,
        "disease_name": disease_name,
        "disease_mesh": mesh,
        "as_of": as_of,
    }
    clients = LiveClients()
    target = {"symbol": target_name, "uniprot_id": "", "recommended": target_name, "names": [target_name]}
    disease = resolve_disease(scope, clients)
    out_dir.mkdir(parents=True, exist_ok=True)
    store_path = out_dir / "sentences.json"
    if store_path.is_file():
        rows = json.loads(store_path.read_text(encoding="utf-8"))
        print(f"store {len(rows)}", flush=True)
    else:
        print("harvest target", flush=True)
        target_papers = harvest(clients, _or_query([target_name]), as_of, SIDE_CAP)
        print("harvest disease", flush=True)
        disease_papers = harvest(clients, _or_query(disease["names"]), as_of, SIDE_CAP)
        rows = paper_sentences(target_papers, [target_name], "target")
        rows.extend(paper_sentences(disease_papers, disease["names"], "disease"))
        store_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"store {len(rows)}", flush=True)

    def save_store() -> None:
        store_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def link(left: str, right: str) -> list[dict]:
        found = gap_search(clients, rows, left, right, as_of)
        save_store()
        return found

    target_rows = [row for row in rows if row["side"] == "target"]
    disease_rows = [row for row in rows if row["side"] == "disease"]
    node_path = out_dir / "nodes.json"
    if node_path.is_file():
        saved = json.loads(node_path.read_text(encoding="utf-8"))
        downstream = [(item["name"], item["sentence"]) for item in saved.get("downstream") or []]
        pathology = [(item["name"], item["sentence"]) for item in saved.get("pathology") or []]
        print(f"nodes downstream {len(downstream)} pathology {len(pathology)}", flush=True)
    else:
        print("read target nodes", flush=True)
        downstream = read_nodes(ask_model, target_rows, target_name, "downstream effect of the target")
        print("read disease nodes", flush=True)
        pathology = []
        disease_names = {name.casefold() for name in disease["names"]}
        for span, row in read_nodes(ask_model, disease_rows, disease["preferred"], "upstream mechanism of the disease"):
            if span.casefold() in disease_names:
                continue
            pathology.append((span, row))
        node_path.write_text(
            json.dumps(
                {
                    "downstream": [{"name": name, "sentence": row} for name, row in downstream],
                    "pathology": [{"name": name, "sentence": row} for name, row in pathology],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"nodes downstream {len(downstream)} pathology {len(pathology)}", flush=True)

    def evidence_from_library(side_rows: list[dict], anchors: list[str], span: str) -> list[dict]:
        return library_hits(side_rows, anchors, span)

    paths: list[dict] = []

    def expand(left: str, hops: list[dict], depth: int) -> None:
        if depth >= DEPTH_CAP:
            return
        failed: list[str] = []
        verified = []
        # ponytail: at most 12 missed names from one left end, then keep the verified ones.
        # Upgrade: let the model stop itself with an empty NODE list only.
        while len(failed) < 12:
            proposals = propose(
                ask_model,
                "next node from the left toward the disease",
                left,
                disease["preferred"],
                failed,
                hops,
                [name for name, _row in pathology],
            )
            seen = {item.casefold() for item in failed}
            seen.update(name.casefold() for name, _row in verified)
            fresh = [name for name in proposals if name.casefold() not in seen]
            if not fresh:
                break
            for name in fresh:
                role = disease_role(ask_model, name, disease["preferred"])
                if role == "other":
                    print(f"drop disease {name}", flush=True)
                    failed.append(name)
                    continue
                if role == "end":
                    hits = link(left, disease["preferred"])
                    if hits:
                        paths.append({"hops": hops + [{"left": left, "right": disease["preferred"], "papers": hits}]})
                        print(f"reached {left}", flush=True)
                    else:
                        failed.append(name)
                    continue
                hits = link(left, name)
                if not hits:
                    print(f"miss {left} -> {name}", flush=True)
                    failed.append(name)
                    continue
                verified.append((name, hits[0]))
            if verified:
                break
        chosen = rank_nodes(ask_model, verified, disease["preferred"])
        for name in chosen:
            hits = link(left, name)
            step = {"left": left, "right": name, "papers": hits}
            expand(name, hops + [step], depth + 1)

    for name, _row in downstream:
        hits = evidence_from_library(target_rows, [target_name], name)
        if not hits:
            continue
        print(f"downstream {name}", flush=True)
        expand(name, [{"left": target_name, "right": name, "papers": hits}], 1)

    report = render(target, disease, downstream, pathology, paths)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(
            {
                "target": target,
                "disease": disease,
                "downstream": [name for name, _row in downstream],
                "pathology": [name for name, _row in pathology],
                "paths": paths,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"paths {len(paths)} -> {out_dir / 'report.md'}", flush=True)


def _paper_line(row: dict) -> str:
    ident = row["pmid"]
    if row.get("doi"):
        ident = f"{ident}, {row['doi']}"
    when = row["date"] or "no day"
    if not quote_in_text(row["sentence"], row["sentence"]):
        return ""
    return f"  - {ident} ({when}). {row['sentence']}"


def render(target: dict, disease: dict, downstream, pathology, paths: list[dict]) -> str:
    lines = [
        f"# {target['symbol']} — {disease['preferred']}",
        "",
        f"UniProt {target['uniprot_id']}. MeSH {disease['mesh']}.",
        "",
        "## Target side",
        "",
    ]
    if not downstream:
        lines.append("No downstream node had a sentence.")
    for name, row in downstream:
        lines.append(f"- {name}")
        lines.append(_paper_line(row))
    lines.extend(["", "## Disease side", ""])
    if not pathology:
        lines.append("No pathology node had a sentence.")
    for name, row in pathology:
        lines.append(f"- {name}")
        lines.append(_paper_line(row))
    lines.extend(["", "## Paths", ""])
    if not paths:
        lines.append("No path reached the disease.")
    for index, path in enumerate(paths, 1):
        lines.append(f"### Path {index}")
        lines.append("")
        for hop in path["hops"]:
            lines.append(f"- {hop['left']} → {hop['right']}")
            for row in hop["papers"]:
                lines.append(_paper_line(row))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _self_check() -> None:
    assert split_sentences("Rate is 1.3 percent. Next claim.") == [
        "Rate is 1.3 percent.",
        "Next claim.",
    ]
    joined = split_sentences("Marker drives alpha\r\nstep in cells. Next claim.")
    assert joined[0] == "Marker drives alpha step in cells."
    assert target_named(joined[0], ["alpha step"])
    assert split_sentences("Marker\tdrives  alpha\u00a0step in cells.") == [
        "Marker drives alpha step in cells."
    ]
    assert split_sentences("Marker drives alp\u00adha\u200b step.") == ["Marker drives alpha step."]
    assert split_sentences("Marker drives alpha-step in cells.") == ["Marker drives alpha-step in cells."]
    assert not target_named(split_sentences("Marker drives alpha-step in cells.")[0], ["alpha step"])
    assert split_sentences("Marker drives alp-\nha step.") == ["Marker drives alp- ha step."]
    verified = ["B cells", "CD40", "IL-21", "BAFF"]
    assert keep_ranked(verified, ["IL-21", "CD40", "missing", "B cells"]) == ["IL-21", "CD40", "B cells"]
    assert keep_ranked(["only"], ["other"]) == ["only"]
    assert both_present("memory T cells need Kv1.3 here.", "Kv1.3", "memory T cells")
    assert not both_present(
        "T cells play a role in their activation.",
        "Kv1.3",
        "T cell activation",
    )
    sentences = [{"sentence": str(i)} for i in range(12)]
    assert [row["sentence"] for row in take_evidence(sentences)] == [str(i) for i in range(10)]
    assert pair_query("Kv1.3", "T cell activation") == '("Kv1.3"[tiab]) AND ("T cell activation"[tiab])'
    print("self-check ok", flush=True)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="Kv1.3")
    parser.add_argument("--disease-name", default="")
    parser.add_argument("--mesh", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--as-of", default="2026-04-30")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return
    if not args.disease_name or not args.mesh or not args.out:
        raise SystemExit("need --disease-name, --mesh, and --out")
    walk(
        Path(args.out),
        target_name=args.target,
        disease_name=args.disease_name,
        mesh=args.mesh,
        as_of=date.fromisoformat(args.as_of),
    )


if __name__ == "__main__":
    _self_check()
    main()
