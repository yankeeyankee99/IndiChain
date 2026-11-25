"""Indicator3 entry: target name in. No disease name input."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re

import yaml

from libraries import (
    LiveClients,
    Paper,
    merge_by_id,
    take_qualified,
    _http_json,
)
from store import add_record, latest_papers_payload, open_store
from c20_reviews import harvest_disease_reviews

SOURCES = ("pubmed", "epmc", "bohrium")
TARGET_PAPER_KEEP = 200
# Copy and judgment both use DeepSeek-V4-Pro. Thinking is enabled on every call.
MODEL_PRO = "deepseek-v4-pro"
_CODING_URL = "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"
_GATEWAY_URL = "https://open.bohrium.com/openapi/model-gateway/v1/chat/completions"
# The gateway rejects the unversioned pro name.
_GATEWAY_PRO = "deepseek-v4-pro-0813"


def load_scope(path: str | Path) -> dict:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = _apply_extends(path, data)
    scope = {
        "harvest_and": [str(x) for x in (data.get("harvest_and") or []) if x],
        "literature_sources": [
            str(x) for x in (data.get("literature_sources") or SOURCES) if x
        ],
        "reading_question": str(data.get("reading_question") or ""),
        "reading_target_lock": bool(data.get("reading_target_lock")),
        "reading_screen": str(data.get("reading_screen") or ""),
        "link_suggest": str(data.get("link_suggest") or ""),
        "target": str(data.get("target") or ""),
        "target_names": [str(x) for x in (data.get("target_names") or []) if str(x).strip()],
        "as_of": _as_of(data.get("as_of")),
        "exclude_known": bool(data.get("exclude_known", True)),
        "chains": int(data.get("chains") or 1),
        "nodes": int(data.get("nodes") or 5),
        "reading_line": str(data.get("reading_line") or "ID | cell | process | molecule | sentence"),
        "disease_mesh": str(data.get("disease_mesh") or ""),
        "disease_name": str(data.get("disease_name") or ""),
        "case_root": str(data.get("case_root") or ""),
        "use_uniprot": bool(data.get("use_uniprot", True)),
        "model_copy": str(data.get("model_copy") or MODEL_PRO),
        "model_judge": str(data.get("model_judge") or MODEL_PRO),
    }
    if data.get("new_indication") is False:
        scope["link_suggest"] = _without_new_indication(scope["link_suggest"])
    return scope


def _apply_extends(path: Path, data: dict) -> dict:
    parent = str(data.get("extends") or "").strip()
    if not parent:
        return data
    base = yaml.safe_load((path.parent / parent).read_text(encoding="utf-8")) or {}
    for key in data.get("omit") or []:
        base.pop(str(key), None)
    if "as_of" not in data:
        base.pop("as_of", None)
    for key, value in data.items():
        if key in ("extends", "omit"):
            continue
        base[key] = value
    return base


def _without_new_indication(text: str) -> str:
    # ponytail: exact clause deletion; if the full prompt rewords these three clauses, the cut misses them.
    for piece in (
        ", and judge it as not a new indication",
        ", judged as not a new indication",
        "When a confirmed step and an inferred step share an axis, that axis is an indication of this target.",
    ):
        text = text.replace(piece, "")
    return text


def _as_of(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text)


def reading_ask(question: str, line_format: str, abstracts: str) -> str:
    return (
        question.strip()
        + "\n\nUse only the abstracts below. One line each: "
        + line_format.strip()
        + "\nCopy the sentence from that abstract. If none of the abstracts states this, answer NONE.\n\n"
        + abstracts
    )


def reading_question(template: str, names: list[str]) -> str:
    shown = ", ".join(dict.fromkeys(n for n in names if n))
    return template.format(names=shown)


def _fold_target_text(text: str) -> str:
    return text.casefold()


def reading_screen(task: str, lines: list[str], names: list[str] | None = None) -> str:
    if names and "{names}" in task:
        task = reading_question(task, names)
    body = "\n".join(line.strip() for line in lines if line.strip())
    return f"{task.strip()}\n\n{body}"


_ITEM_EDGE = re.compile(r"[,.;:!?()\[\]\"'“”‘’]")


def _comma_name_hit(folded: str, name: str) -> bool:
    """A comma-form name hits only when its head starts that comma item.

    ponytail: a word before the head, including a function word, is not a hit.
    Upgrade: treat function words as outside the name.
    """
    token = re.escape(name)
    for match in re.finditer(rf"(?<![a-z0-9]){token}(?![a-z0-9])", folded):
        left = _ITEM_EDGE.split(folded[: match.start()])[-1]
        if not left.strip():
            return True
    return False


def target_named(text: str, names: list[str]) -> bool:
    folded = _fold_target_text(text)
    for name in dict.fromkeys(n for n in names if n):
        token = _fold_target_text(name)
        if "," in token:
            if _comma_name_hit(folded, token):
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", folded):
            return True
    return False


def _target_query(target: dict, harvest_and: list[str] | None = None) -> str:
    names = [target["name"], *(target.get("aliases") or []), target.get("uniprot_id") or ""]
    target_or = " OR ".join(dict.fromkeys(n for n in names if n))
    extra = [t for t in (harvest_and or []) if t]
    if not extra:
        return target_or
    return f"({target_or}) AND ({' OR '.join(extra)})"


def _papers_from_payload(payload: dict) -> list[Paper]:
    papers = []
    for row in payload.get("papers") or []:
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


def run(
    target_name: str,
    *,
    as_of: date | None,
    clients,
    store=None,
    until: str | None = None,
    harvest_and: list[str] | None = None,
    sources: list[str] | None = None,
    names: list[str] | None = None,
    use_uniprot: bool = True,
) -> str:
    sources = [s for s in (sources or SOURCES) if s]
    if use_uniprot:
        try:
            target = clients.uniprot(target_name)
        except RuntimeError:
            if not names:
                raise
            target = {"name": target_name, "uniprot_id": "", "aliases": list(names)}
    else:
        target = {"name": target_name, "uniprot_id": "", "aliases": list(names or [target_name])}
    listed = [n for n in (names or []) if n]
    if listed:
        target["aliases"] = list(dict.fromkeys([*(target.get("aliases") or []), *listed]))
    print(f"target {target['name']} {target['uniprot_id']}", flush=True)
    query = _target_query(target, harvest_and)
    print(f"query {query}", flush=True)
    papers: list[Paper] = []
    replayed = False
    if store is not None and until != "papers":
        payload = latest_papers_payload(
            store, query=query, as_of=as_of, harvest_and=harvest_and
        )
        if payload:
            papers = _papers_from_payload(payload)
            replayed = True
            print(f"replay papers {len(papers)}", flush=True)
    if not replayed:
        groups = []
        source_n = {}
        for source in sources:
            raw = clients.search_papers(query, as_of=as_of, source=source, keep=TARGET_PAPER_KEEP)
            kept = take_qualified(raw, as_of=as_of, keep=TARGET_PAPER_KEEP, can_page=True)
            source_n[source] = len(kept)
            groups.append(kept)
            print(f"search {source} {len(kept)}", flush=True)
        papers = merge_by_id(groups)
        print(f"papers {len(papers)}", flush=True)
        if store is not None:
            add_record(
                store,
                kind="papers",
                payload={
                    "query": query,
                    "harvest_and": list(harvest_and or []),
                    "sources": list(sources),
                    "n": len(papers),
                    "by_source": source_n,
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
    return f"papers {len(papers)}"


def _load_dotenv() -> None:
    files = (
        Path.home() / ".env",
        Path.home() / "env.txt",
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env.txt",
    )
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_LAST_REASONING = ""


def last_reasoning() -> str:
    return _LAST_REASONING


# Gateway models that publish reasoning effort levels. The top level is max.
_EFFORT_MAX = {
    "kimi-k3.1-pro": "max",
}


def _chat_payload(prompt: str, model: str, messages: list | None = None) -> dict:
    payload = {
        "model": model,
        "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
        "temperature": 0,
        "thinking": {"type": "enabled"},
    }
    effort = _EFFORT_MAX.get(model)
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _llm_target(model: str) -> tuple[str, str, str]:
    """Accesskey uses the Bohrium model gateway. Otherwise the Coding Plan key."""
    access = (os.environ.get("Accesskey") or os.environ.get("BOHR_ACCESS_KEY") or "").strip()
    if access:
        override = (os.environ.get("INDICATOR_MODEL") or "").strip()
        if override:
            sent = override
        else:
            sent = _GATEWAY_PRO if model in (MODEL_PRO, "deepseek-v4-flash", _GATEWAY_PRO) else model
        return _GATEWAY_URL, access, sent
    key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("LLM calls need ARK_API_KEY")
    return _CODING_URL, key, model


def _deepseek(
    prompt: str,
    *,
    model: str = MODEL_PRO,
    timeout: int = 180,
    messages: list | None = None,
) -> str:
    global _LAST_REASONING
    url, key, sent = _llm_target(model)
    payload = _http_json(
        url,
        data=json.dumps(_chat_payload(prompt, sent, messages)).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    message = payload["choices"][0]["message"]
    _LAST_REASONING = str(message.get("reasoning_content") or "")
    return str(message.get("content") or "")


def main(argv: list[str] | None = None) -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="run")
    parser.add_argument("target", nargs="?", default="")
    parser.add_argument("--f-mesh", default=None, help="MeSH ID for --until e")
    parser.add_argument(
        "--as-of",
        default=None,
        help="YYYY-MM-DD; omitted uses the scope date, which may be unset",
    )
    parser.add_argument(
        "--store",
        default=str(Path(__file__).resolve().parent / "run.sqlite"),
    )
    parser.add_argument(
        "--scope",
        default=str(Path(__file__).resolve().parent / "scope.yaml"),
        help="YAML scope; empty harvest_and is target-only",
    )
    parser.add_argument(
        "--until",
        default=None,
        choices=["papers", "e"],
        help="papers: stop after literature; e: one disease's reviews",
    )
    args = parser.parse_args(argv)
    if args.until == "e" and not args.f_mesh:
        parser.error("--until e requires --f-mesh")
    if args.until != "e" and not args.target:
        parser.error("target is required unless --until e")
    scope = load_scope(args.scope)
    as_of = date.fromisoformat(args.as_of) if args.as_of else scope.get("as_of")
    store = open_store(args.store) if args.store else None
    if args.until == "e":
        papers = harvest_disease_reviews(
            mesh_id=args.f_mesh,
            as_of=as_of,
            clients=LiveClients(),
            sources=scope["literature_sources"],
            store=store,
        )
        print(f"e_reviews {len(papers)}", flush=True)
        return
    text = run(
        args.target,
        as_of=as_of,
        clients=LiveClients(),
        store=store,
        until=args.until,
        harvest_and=scope["harvest_and"],
        sources=scope["literature_sources"],
    )
    print(text)


if __name__ == "__main__":
    main()
