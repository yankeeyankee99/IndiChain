"""One named target and one named disease. PubMed abstracts, then the chain."""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

from urllib.error import HTTPError

from libraries import LiveClients, disease_review_query
from llm import (
    NODE_RULE,
    build_named_chain,
    http_detail,
    http_reason,
    link_sources,
    parse_query,
    query_question,
    quote_in_text,
)
from report_case import render_case
from run import MODEL_PRO, _deepseek, _load_dotenv, _target_query, load_scope, target_named

ROOT = Path(__file__).resolve().parent
MODEL = MODEL_PRO
AS_OF = None
# ponytail: at most 24 hits of the model's PubMed search are read. A later hit is missed; raise PAPER_KEEP.
PAPER_KEEP = 24
BATCH = 8
_LIB_LINE = re.compile(r"^\[(\d+)\]\t(.+)$")


def pubmed_pair_query(left: str, right: str) -> str:
    return f"({left}) AND ({right})"


def target_end_query(target: dict, harvest_and: list[str] | None) -> str:
    return _target_query(target, harvest_and)


def disease_end_query(name: str, mesh_id: str) -> str:
    return disease_review_query(name, "pubmed", mesh_id)


def accept_library_line(line: str, papers: dict[str, str], names: list[str], *, side: str) -> str | None:
    match = _LIB_LINE.match((line or "").strip())
    if not match:
        return None
    pmid, quote = match.group(1), match.group(2).strip()
    if not quote_in_text(quote, papers.get(pmid) or ""):
        return None
    if side == "target" and not target_named(quote, names):
        return None
    return quote


def ask(prompt: str, *, timeout: int = 900) -> str:
    return _deepseek(prompt, model=MODEL, timeout=timeout)


def case_route(scope: dict) -> str:
    if str(scope.get("disease_mesh") or "").strip() and str(scope.get("target") or "").strip():
        return "named"
    return "idle"


def named_case_store(scope: dict) -> Path:
    target = str(scope.get("target") or "case").lower()
    mesh = str(scope.get("disease_mesh") or "disease")
    base = str(scope.get("case_root") or "").strip()
    root = (ROOT / base) if base else (ROOT / "data" / "named")
    return root / target / mesh / "run.sqlite"


def named_case_paths(scope: dict, run_label: str = "") -> dict[str, Path]:
    folder = named_case_store(scope).parent
    paths = {
        "store": folder / "run.sqlite",
        "target_library": folder / "target_library.json",
        "disease_library": folder / "disease_library.json",
        "chain": folder / "chain.json",
        "report_md": folder / "report.md",
        "report_json": folder / "report.json",
        "report_html": folder / "report.html",
    }
    if not run_label:
        return paths
    run_folder = folder / run_label
    for key in ("chain", "report_md", "report_json", "report_html"):
        paths[key] = run_folder / paths[key].name
    return paths


def _sentences(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [row for row in (data.get("sentences") or []) if str(row.get("quote") or "").strip()]


def _records(rows: list[dict]) -> list[dict]:
    return [{**row, "lines": [str(row.get("quote") or "")]} for row in rows]


def chain_steps_for_report(steps: list[dict]) -> list[dict]:
    out = []
    for step in steps:
        text = str(step.get("step") or "")
        left, right = text, ""
        for sep in ("→", "->"):
            if sep in text:
                left, right = [part.strip() for part in text.split(sep, 1)]
                break
        quotes = [str(q) for q in (step.get("quotes") or []) if str(q).strip()]
        library = ""
        if step.get("side") and step.get("n"):
            library = f"{step['side']} {step['n']}"
        out.append({
            "left": left,
            "right": right,
            "status": step.get("status") or "",
            "quote": quotes[0] if quotes else "",
            "paper_id": str(step.get("paper_id") or ""),
            "title": str(step.get("title") or ""),
            "library": library,
        })
    return out


def _endpoint_target(scope: dict, clients: LiveClients) -> tuple[dict, list[str]]:
    names = [str(item) for item in (scope.get("target_names") or []) if str(item).strip()]
    target_name = str(scope.get("target") or "")
    if scope.get("use_uniprot", True):
        try:
            target = clients.uniprot(target_name)
        except RuntimeError:
            target = {"name": target_name, "uniprot_id": "", "aliases": list(names)}
    else:
        target = {"name": target_name, "uniprot_id": "", "aliases": list(names or [target_name])}
    if names:
        target["aliases"] = list(dict.fromkeys([*(target.get("aliases") or []), *names]))
    shown = names or [str(target.get("name") or target_name)]
    return target, shown


def _library_task(side: str, target: str, disease: str, papers: list) -> str:
    blocks = [f"[{paper.paper_id}]\n{paper.text}" for paper in papers]
    return (
        "TASK library\n"
        f"Side: {side}\nTarget: {target}\nDisease: {disease}\n"
        + NODE_RULE + "\n"
        + "Copy each abstract sentence that states such a node.\n"
        "One line: the paper id in brackets, tab, the sentence copied from that abstract.\n"
        "If none of the abstracts states a node, write NONE.\n\n"
        + "\n\n".join(blocks)
    )


def _collect(papers: list, ask_model, side: str, target: str, disease: str, names: list[str]) -> list[dict]:
    by_id = {str(paper.paper_id): paper.text or "" for paper in papers}
    titles = {str(paper.paper_id): paper.title or "" for paper in papers}
    rows = []
    seen = set()
    for start in range(0, len(papers), BATCH):
        chunk = papers[start : start + BATCH]
        reply = ask_model(_library_task(side, target, disease, chunk))
        for line in (reply or "").splitlines():
            quote = accept_library_line(line, by_id, names, side=side)
            match = _LIB_LINE.match(line.strip())
            if not quote or not match:
                continue
            key = quote.casefold()
            if key in seen:
                continue
            seen.add(key)
            pmid = match.group(1)
            rows.append({
                "n": len(rows) + 1,
                "paper_id": pmid,
                "title": titles.get(pmid) or "",
                "quote": quote,
            })
    return rows


def _search_with(clients: LiveClients):
    def search(query: str) -> list:
        return _search(clients, query)

    return search


def _search(clients: LiveClients, query: str) -> list:
    papers = clients.search_papers(query, as_of=AS_OF, source="pubmed", keep=PAPER_KEEP, strict=True)
    return [paper for paper in papers if (paper.text or "").strip() and (paper.text or "").strip() != (paper.title or "").strip()]


def _write_library(path: Path, query: str, sentences: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {"done": True, "source": "pubmed", "query": query, "sentences": sentences},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def model_search(search, ask_model, left: str, right: str) -> tuple[str, list]:
    """The model writes the PubMed search. Three rejections stop this search."""
    rejected = ""
    for _attempt in range(3):
        query = parse_query(ask_model(query_question(left, right, rejected)))
        if not query:
            rejected = "The reply had no QUERY line."
            continue
        try:
            return query, search(query)
        except HTTPError as exc:
            if http_reason(exc) != "query_rejected":
                raise
            rejected = http_detail(exc) or "PubMed rejected the search."
    return "", []


def build_libraries(scope: dict, paths: dict[str, Path]) -> None:
    paths["target_library"].parent.mkdir(parents=True, exist_ok=True)
    clients = LiveClients()
    target, names = _endpoint_target(scope, clients)
    disease = str(scope.get("disease_name") or "")
    shown = str(target.get("name") or scope.get("target") or "")
    target_query, target_papers = model_search(_search_with(clients), ask, shown, "downstream nodes")
    disease_query, disease_papers = model_search(_search_with(clients), ask, disease, "pathology")
    print(f"query target {target_query}", flush=True)
    print(f"query disease {disease_query}", flush=True)
    print(f"pubmed target {len(target_papers)} disease {len(disease_papers)}", flush=True)
    target_rows = _collect(target_papers, ask, "target", shown, disease, names)
    disease_rows = _collect(disease_papers, ask, "disease", shown, disease, names)
    _write_library(paths["target_library"], target_query, target_rows)
    _write_library(paths["disease_library"], disease_query, disease_rows)
    print(f"sentences target {len(target_rows)} disease {len(disease_rows)}", flush=True)


def link_named(scope: dict, records: list[dict], disease_rows: list[dict], name: str, mesh_id: str) -> dict:
    def fetch(query: str, source: str) -> list[dict]:
        if source not in link_sources():
            return []
        papers = LiveClients().search_papers(
            query,
            as_of=AS_OF,
            source="pubmed",
            keep=20,
            strict=True,
        )
        return [
            {"paper_id": paper.paper_id, "title": paper.title, "text": paper.text or "", "source": paper.source}
            for paper in papers
        ]

    aliases = [str(item) for item in (scope.get("target_names") or []) if str(item).strip()]
    built = build_named_chain(
        target=aliases[0] if aliases else str(scope.get("target") or ""),
        disease=name,
        records=records,
        disease_rows=disease_rows,
        direction="",
        ask=lambda prompt: ask(prompt, timeout=900),
        fetch=fetch,
        cap=int(scope.get("nodes") or 5),
    )
    steps = built["steps"]
    return {
        "mesh_id": mesh_id,
        "name": name,
        "groups": built.get("paths") or ([steps] if steps else []),
        "steps": steps,
        "paths": built.get("paths") or [],
        "path": "",
        "retrievals": built["events"],
        "raw": "",
        "no_axis": built["stop"],
        "stop": built["stop"],
        "reached": built["reached"],
        "disease_end": built.get("disease_end"),
        "audit": built.get("audit") or [],
        "ends_reason": built.get("ends_reason") or "",
    }


def write_named_report(scope: dict, paths: dict[str, Path], chain: dict) -> None:
    as_of = scope.get("as_of")
    shown = chain_steps_for_report(chain.get("steps") or [])
    end_raw = chain.get("disease_end")
    shown_end = None
    if end_raw:
        shown_end = chain_steps_for_report([end_raw])[0]
        shown_end["linked"] = bool(end_raw.get("linked"))
    text, page, payload = render_case({
        "target": scope.get("target") or "",
        "target_id": "",
        "disease": scope.get("disease_name") or "",
        "disease_mesh": scope.get("disease_mesh") or "",
        "sources": ["pubmed"],
        "as_of": as_of.isoformat() if as_of else "",
        "path": chain.get("path") or "",
        "stop": chain.get("stop") or "",
        "ends_reason": chain.get("ends_reason") or "",
        "chain": shown,
        "disease_end": shown_end,
    })
    route_list = []
    for path in chain.get("paths") or []:
        route_list.append([str(step.get("step") or "") for step in path])
    if len(route_list) > 1:
        text += "\n## Paths\n" + "\n".join(
            f"{index}. " + " | ".join(route) for index, route in enumerate(route_list, start=1)
        ) + "\n"
    payload["paths"] = route_list
    paths["report_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["report_md"].write_text(text, encoding="utf-8")
    paths["report_json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path = paths.get("report_html") or paths["report_md"].with_name("report.html")
    html_path.write_text(page, encoding="utf-8")


def _libraries_done(paths: dict[str, Path]) -> bool:
    for key in ("target_library", "disease_library"):
        path = paths[key]
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        if not data.get("done"):
            return False
    return True


def _link_from_libraries(scope: dict, paths: dict[str, Path]) -> dict:
    mesh_id = str(scope["disease_mesh"])
    records = _records(_sentences(paths["target_library"]))
    disease_rows = _records(_sentences(paths["disease_library"]))
    if records and disease_rows:
        return link_named(scope, records, disease_rows, str(scope.get("disease_name") or ""), mesh_id)
    return {
        "mesh_id": mesh_id,
        "name": scope.get("disease_name") or "",
        "steps": [],
        "groups": [],
        "path": "",
        "stop": "library empty",
        "no_axis": "library empty",
    }


def relink_named_case(scope: dict, run_label: str = "") -> None:
    paths = named_case_paths(scope, run_label)
    if not _libraries_done(paths):
        raise SystemExit(f"libraries unfinished, not linking: {paths['target_library'].parent}")
    chain = _link_from_libraries(scope, paths)
    paths["chain"].parent.mkdir(parents=True, exist_ok=True)
    paths["chain"].write_text(json.dumps(chain, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_named_report(scope, paths, chain)
    if chain.get("stop") == "model_unavailable":
        raise SystemExit("model_unavailable: batch paused")
    print(
        f"relink {scope['target']} {scope['disease_mesh']} "
        f"steps {len(chain.get('steps') or [])} stop {chain.get('stop') or ''}",
        flush=True,
    )


def run_named_case(scope: dict, *, link: bool = True) -> None:
    paths = named_case_paths(scope)
    if not _libraries_done(paths):
        build_libraries(scope, paths)
    if not link:
        print(f"libraries {scope['target']} {scope['disease_mesh']} {paths['target_library'].parent}", flush=True)
        return
    chain = _link_from_libraries(scope, paths)
    paths["chain"].write_text(json.dumps(chain, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_named_report(scope, paths, chain)
    if chain.get("stop") == "model_unavailable":
        raise SystemExit("model_unavailable: batch paused")
    print(
        f"named {scope['target']} {scope['disease_mesh']} "
        f"steps {len(chain.get('steps') or [])} stop {chain.get('stop') or ''}",
        flush=True,
    )


def _scope_file() -> Path:
    if "--scope" in sys.argv:
        return Path(sys.argv[sys.argv.index("--scope") + 1])
    return ROOT / "scope.yaml"


def _self_check() -> None:
    assert pubmed_pair_query("ALAS1", "IgAN") == "(ALAS1) AND (IgAN)"
    assert link_sources() == ("pubmed",)
    assert target_end_query({"name": "Kv1.3", "aliases": ["KCNA3"], "uniprot_id": ""}, []) == "Kv1.3 OR KCNA3"
    assert disease_end_query("Pemphigus", "D010392") == '"Pemphigus"[Majr] AND review[pt]'
    abstract = "Kv1.3 blockade reduced T cell activation in vitro. Patients improved."
    papers = {"1": abstract}
    assert accept_library_line(
        "[1]\tKv1.3 blockade reduced T cell activation in vitro.", papers, ["Kv1.3"], side="target"
    ) == "Kv1.3 blockade reduced T cell activation in vitro."
    assert accept_library_line("[1]\tPatients improved.", papers, ["Kv1.3"], side="target") is None
    assert case_route({"disease_mesh": "D017118", "target": "ALAS1"}) == "named"
    assert case_route({}) == "idle"
    print("self-check ok", flush=True)


def main() -> None:
    if "--self-check" in sys.argv:
        _self_check()
        return
    _load_dotenv()
    scope = load_scope(_scope_file())
    global AS_OF, MODEL
    AS_OF = scope.get("as_of")
    MODEL = str(scope.get("model_judge") or MODEL_PRO)
    if case_route(scope) != "named":
        print("scope has no named target and disease", flush=True)
        return
    run_label = ""
    if "--run" in sys.argv:
        run_label = sys.argv[sys.argv.index("--run") + 1]
    if "--link-only" in sys.argv:
        relink_named_case(scope, run_label)
    elif "--libraries-only" in sys.argv:
        run_named_case(scope, link=False)
    else:
        run_named_case(scope)


if __name__ == "__main__":
    main()
