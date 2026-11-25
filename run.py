"""Campaign entry. Target name and freeze date in. One batch directory out.

A batch lives at campaigns/<target>/<timestamp>/.
The freeze date is the one typed for this run. It is not taken from the target name.
nominate, the mechanism lists, and chain all use that date.
nominate runs five times, then writes nominate/top5.md.
merge resolves names to MeSH ids. The same id is one row, ordered by how many runs named it.
mesh keeps the first five matched rows of that table and writes scopes/ plus top5.md.
A failed row is skipped. The next row fills that place.
chain reads those scopes and writes only this batch's chains/.
The default target is Kv1.3. Another target gets a generated biological context before the five runs.
score reads only that batch's chains/ and writes score/ there.

Historical files under chain/data and chain/top5 are not a batch.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
MODEL = "deepseek-v4-pro"


def _check_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"as-of must be YYYY-MM-DD: {value}")
    return value


def _run(cmd: list[str], cwd: Path) -> None:
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd))
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def open_batch(target: str, as_of: str, stamp: str = "") -> Path:
    as_of = _check_date(as_of)
    if not as_of:
        raise SystemExit("pass --as-of YYYY-MM-DD")
    name = _campaign_name(target)
    when = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    if MODEL != "deepseek-v4-pro":
        when = f"{when}-{_campaign_name(MODEL)}"
    batch = ROOT / "campaigns" / name / when
    if batch.exists():
        raise SystemExit(f"batch already exists: {batch}")
    (batch / "nominate").mkdir(parents=True)
    (batch / "chains").mkdir(parents=True)
    meta = {
        "target": target,
        "model": MODEL,
        "thinking": "enabled",
        "as_of": as_of,
    }
    (batch / "batch.json").write_text(
        json.dumps(meta, indent=2) + "\n",
        encoding="utf-8",
    )
    return batch


def _swap_context(text: str, name: str, end_marker: str) -> str:
    """Another target keeps the skeleton and receives its own biological context."""
    marker = "Biological context\n"
    start = text.find(marker)
    end = text.find(end_marker)
    if start < 0 or end < start:
        return text.replace("Kv1.3", name)
    head = text[:start].replace("Kv1.3", name)
    tail = text[end:].replace("Kv1.3", name)
    return head + marker + "<<<BIOLOGICAL_CONTEXT>>>\n" + tail


def nominate_prompt(batch: Path, target: str, source: Path | None = None) -> Path:
    """Nomination reads one prompt and prompt4_lists.md. The batch supplies the freeze date."""
    src = source or (ROOT / "nominate" / "prompt4.md")
    lists_src = ROOT / "nominate" / "prompt4_lists.md"
    if not src.is_file() or not lists_src.is_file():
        raise SystemExit(f"nomination prompts missing beside {src}")
    meta_path = batch / "batch.json"
    if not meta_path.is_file():
        raise SystemExit(f"not a batch: {batch}")
    as_of = str(json.loads(meta_path.read_text(encoding="utf-8")).get("as_of") or "")
    if not as_of:
        raise SystemExit("pass --as-of YYYY-MM-DD")
    sys.path.insert(0, str(ROOT / "nominate"))
    import batch_top5

    frozen = batch_top5.frozen_target(target)
    name = target.strip()
    if not frozen and not name:
        raise SystemExit("nomination needs a target name")

    def prepare(text: str, end_marker: str) -> str:
        if not frozen:
            text = _swap_context(text, name, end_marker)
        if "<<<AS_OF>>>" not in text:
            raise SystemExit("prompt is missing <<<AS_OF>>>")
        return text.replace("<<<AS_OF>>>", as_of)

    dest = batch / "nominate" / "prompt4.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(prepare(src.read_text(encoding="utf-8"), "\nClosed mechanism lists"), encoding="utf-8")
    dest.with_name("prompt4_lists.md").write_text(
        prepare(lists_src.read_text(encoding="utf-8"), "\nUse these headings"),
        encoding="utf-8",
    )
    return dest


def _campaign_name(target: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in target.strip()) or "target"


def latest_ready_batch(target: str) -> Path:
    root = ROOT / "campaigns" / _campaign_name(target)
    if not root.is_dir():
        raise SystemExit(f"no campaign for {target}")
    ready = [
        path
        for path in root.iterdir()
        if (path / "batch.json").is_file() and any((path / "nominate" / "scopes").glob("*.yaml"))
    ]
    if not ready:
        raise SystemExit(f"chain needs a nominate top5 for {target}. Run --stage nominate first.")
    return sorted(ready)[-1]


def fill_context(batch: Path, target: str) -> None:
    """A non-Kv1.3 batch has an empty biological context. One model call fills both prompts."""
    prompt_path = batch / "nominate" / "prompt4.md"
    lists_path = prompt_path.with_name("prompt4_lists.md")
    if "<<<BIOLOGICAL_CONTEXT>>>" not in prompt_path.read_text(encoding="utf-8"):
        return
    sys.path.insert(0, str(ROOT / "nominate"))
    import run_agent_plan

    ask = (ROOT / "nominate" / "prompt_context.md").read_text(encoding="utf-8").replace("<<<TARGET>>>", target)
    body = run_agent_plan.request_body(MODEL, ask, 16000, False, "enabled", None)
    payload, error, _elapsed = run_agent_plan.post(body, False)
    if payload is None:
        raise SystemExit(error or "biological context failed")
    content, _reasoning, finish = run_agent_plan.message_parts(payload)
    paragraph = content.strip()
    if finish != "stop" or not paragraph:
        raise SystemExit("biological context not generated")
    for path in (prompt_path, lists_path):
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("<<<BIOLOGICAL_CONTEXT>>>", paragraph), encoding="utf-8")


def _context_pending(prompt: Path, batch: Path) -> bool:
    return prompt.resolve() == (batch / "nominate" / "prompt4.md").resolve()


def bind_nominate_prompt(batch: Path, name: str) -> Path:
    """One batch keeps the nomination prompt it started with."""
    meta_path = batch / "batch.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stored = str(meta.get("nominate_prompt") or "")
    if not stored:
        stored = name
        meta["nominate_prompt"] = stored
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    elif name not in (stored, "prompt4.md"):
        raise SystemExit(f"batch nomination prompt is {stored}")
    src = (ROOT / "nominate" / stored).resolve()
    if src.parent != (ROOT / "nominate").resolve() or not src.is_file():
        raise SystemExit(f"nomination prompt missing: {stored}")
    return src


def bind_as_of(batch: Path, typed: str) -> None:
    """One batch keeps the freeze date it started with."""
    typed = _check_date(typed)
    meta_path = batch / "batch.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stored = str(meta.get("as_of") or "")
    if typed and stored and typed != stored:
        raise SystemExit(f"batch freeze date is {stored}")
    if typed and not stored:
        meta["as_of"] = typed
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def stage_nominate(batch: Path, run_idx: int, source: Path | None = None) -> None:
    dest = (batch / "nominate" / f"run-{run_idx}").resolve()
    dest.mkdir(parents=True, exist_ok=True)
    target = str(json.loads((batch / "batch.json").read_text(encoding="utf-8")).get("target") or "")
    prompt = nominate_prompt(batch, target, source)
    if _context_pending(prompt, batch):
        fill_context(batch, target)
    _run(
        [
            PYTHON,
            "-u",
            str(ROOT / "nominate" / "run_agent_plan.py"),
            MODEL,
            "--run",
            str(run_idx),
            "--thinking",
            "enabled",
            "--prompt",
            str(prompt),
            "--out",
            str(dest),
        ],
        ROOT / "nominate",
    )


def stage_nominate_round(batch: Path, source: Path | None = None) -> None:
    """Five nomination runs, then the MeSH-merged top 5 that chain reads."""
    target = str(json.loads((batch / "batch.json").read_text(encoding="utf-8")).get("target") or "")
    prompt = nominate_prompt(batch, target, source)
    if _context_pending(prompt, batch):
        fill_context(batch, target)
    script = ROOT / "nominate" / "run_agent_plan.py"
    procs = []
    logs = []
    for n in range(1, 6):
        dest = batch / "nominate" / f"run-{n}"
        dest.mkdir(parents=True, exist_ok=True)
        log = (batch / "nominate" / f"run-{n}.log").open("w", encoding="utf-8")
        logs.append(log)
        procs.append(
            subprocess.Popen(
                [
                    PYTHON,
                    "-u",
                    str(script),
                    MODEL,
                    "--run",
                    str(n),
                    "--thinking",
                    "enabled",
                    "--prompt",
                    str(prompt),
                    "--out",
                    str(dest),
                ],
                cwd=str(ROOT / "nominate"),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        )
    codes = [proc.wait() for proc in procs]
    for log in logs:
        log.close()
    if any(codes):
        raise SystemExit(f"nominate failed: {codes}")
    stage_merge(batch)
    stage_top5(batch)
    stage_mesh(batch)
    print(f"top5 {batch / 'nominate' / 'top5.md'}", flush=True)


def _batch_tools():
    sys.path.insert(0, str(ROOT / "nominate"))
    import batch_top5

    return batch_top5


def stage_merge(batch: Path) -> None:
    sys.path.insert(0, str(ROOT / "nominate"))
    import merge_counts

    out = merge_counts.write_merge(batch)
    print(f"merge wrote {out}", flush=True)


def stage_top5(batch: Path, counts: str = "") -> None:
    if counts:
        path = Path(counts)
    else:
        path = batch / "nominate" / "counts.md"
        if not path.is_file():
            raise SystemExit(
                "top5 failed: no merged count table. Run --stage merge first, "
                "or pass --counts <path> to a written table."
            )
    payload = _batch_tools().write_top5(batch, counts_path=path)
    print(
        f"top5 candidates {len(payload['candidates'])} from {payload['source']} "
        f"-> {batch / 'nominate' / 'top5.json'}",
        flush=True,
    )


def stage_mesh(batch: Path) -> None:
    payload = _batch_tools().write_scopes(batch)
    ready = [row["name"] for row in payload["diseases"] if row.get("status") == "matched"]
    print(
        f"mesh matched {len(ready)} distinct "
        f"(skipped {len(payload.get('skipped') or [])})",
        flush=True,
    )


def library_ready(folder: Path) -> bool:
    """Both sentence libraries exist and the PubMed pass finished."""
    for name in ("target_library.json", "disease_library.json"):
        path = folder / name
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        if not data.get("done"):
            return False
    return True


_SERVICE_STOPS = {
    "model_timeout",
    "ends_unreadable",
    "source_unavailable",
    "abstract_missing",
    "model_unavailable",
}


def _report_stands(report: Path) -> bool:
    """Keep a finished chain. A service stop is run once more, then kept."""
    if not report.is_file():
        return False
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    stop = str(data.get("stop") or "")
    if stop not in _SERVICE_STOPS:
        return True
    marker = report.parent / ".service_retried"
    if marker.is_file():
        return True
    marker.write_text(stop + "\n", encoding="utf-8")
    return False


def stage_chain(batch: Path) -> None:
    top5 = batch / "nominate" / "top5.json"
    if not top5.is_file():
        raise SystemExit(f"chain needs {top5}. Refusing to write into chain/top5.")
    scopes = _batch_tools().matched_scopes(batch)
    if not scopes:
        raise SystemExit(f"chain needs a matched scope under {batch / 'nominate' / 'scopes'}. Nothing was linked.")
    import yaml

    walk = ROOT / "chain" / "walk_live.py"
    chains = (batch / "chains").resolve()
    for scope_path in scopes:
        data = yaml.safe_load(scope_path.read_text(encoding="utf-8")) or {}
        case_root = Path(str(data.get("case_root") or "")).resolve()
        if case_root != chains:
            raise SystemExit(f"{scope_path} case_root is {case_root}, not {chains}")
        mesh = str(data.get("disease_mesh") or "")
        target = str(data.get("target") or "case").lower()
        folder = chains / target / mesh
        ready = library_ready(folder)
        if not ready:
            _run([PYTHON, "-u", str(walk), "--scope", str(scope_path.resolve()), "--libraries-only"], ROOT / "chain")
        if not library_ready(folder):
            raise SystemExit(f"library unfinished, not linking: {folder}")
        for n in range(1, 6):
            report = folder / f"result-{n}" / "report.json"
            if _report_stands(report):
                print(f"skip existing {report}", flush=True)
                continue
            _run(
                [PYTHON, "-u", str(walk), "--scope", str(scope_path.resolve()), "--link-only", "--run", f"result-{n}"],
                ROOT / "chain",
            )


def stage_score(batch: Path) -> None:
    _run(
        [PYTHON, "-u", str(ROOT / "score" / "score_batch.py"), "--batch", str(batch.resolve())],
        ROOT / "score",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="", help="target name; default Kv1.3")
    parser.add_argument("--batch", default="", help="existing campaign directory")
    parser.add_argument(
        "--stage",
        choices=("nominate", "merge", "top5", "mesh", "chain", "score"),
        default="score",
    )
    parser.add_argument("--run", type=int, default=None, help="one nominate index; omit to run all five")
    parser.add_argument("--model", default="", help="nomination model; default deepseek-v4-pro")
    parser.add_argument("--as-of", default="", help="freeze date YYYY-MM-DD for this run")
    parser.add_argument(
        "--prompt",
        default="prompt4.md",
        help="nomination prompt in nominate/; a batch keeps its first choice",
    )
    parser.add_argument(
        "--counts",
        default="",
        help="merged count table for --stage top5; default nominate/counts.md after merge",
    )
    args = parser.parse_args()
    if args.model:
        global MODEL
        MODEL = args.model
    if not args.target and not args.batch and args.stage in {"nominate", "chain"}:
        args.target = "Kv1.3"
    if args.batch:
        batch = Path(args.batch)
        if not (batch / "batch.json").is_file():
            raise SystemExit(f"not a batch: {batch}")
    elif args.stage == "chain":
        if not args.target:
            raise SystemExit("pass a target name or --batch <campaign>")
        batch = latest_ready_batch(args.target)
        print(batch, flush=True)
    else:
        if not args.target:
            raise SystemExit("pass a target name or --batch <campaign>")
        batch = open_batch(args.target, args.as_of)
        print(batch, flush=True)
    bind_as_of(batch, args.as_of)
    if args.stage == "nominate":
        source = bind_nominate_prompt(batch, args.prompt)
        if args.run is None:
            stage_nominate_round(batch, source)
        else:
            stage_nominate(batch, args.run, source)
    elif args.stage == "merge":
        stage_merge(batch)
    elif args.stage == "top5":
        stage_top5(batch, args.counts)
    elif args.stage == "mesh":
        stage_mesh(batch)
    elif args.stage == "chain":
        stage_chain(batch)
    else:
        stage_score(batch)


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "disease_library.json").write_text(
            json.dumps({"done": True, "sentences": []}) + "\n", encoding="utf-8"
        )
        (folder / "target_library.json").write_text(
            json.dumps({"done": False, "sentences": []}) + "\n", encoding="utf-8"
        )
        assert not library_ready(folder)
        (folder / "target_library.json").write_text(
            json.dumps({"done": True, "sentences": []}) + "\n", encoding="utf-8"
        )
        assert library_ready(folder)
        timeout = folder / "result-1"
        timeout.mkdir()
        report = timeout / "report.json"
        report.write_text(json.dumps({"stop": "model_timeout", "steps": []}) + "\n", encoding="utf-8")
        assert not _report_stands(report)
        assert _report_stands(report)
        report.write_text(json.dumps({"stop": "attempt_limit", "steps": []}) + "\n", encoding="utf-8")
        assert _report_stands(report)
    with tempfile.TemporaryDirectory() as tmp:
        batch = Path(tmp)
        (batch / "batch.json").write_text(
            json.dumps({"target": "KCNA3", "as_of": "2026-04-30"}) + "\n", encoding="utf-8"
        )
        kept = nominate_prompt(batch, "KCNA3")
        kept_text = kept.read_text(encoding="utf-8")
        assert kept == batch / "nominate" / "prompt4.md"
        assert "2026-04-30" in kept_text and "<<<AS_OF>>>" not in kept_text
        assert "Kv1.3" in kept_text
        (batch / "batch.json").write_text(
            json.dumps({"target": "PCSK9", "as_of": "2018-06-01"}) + "\n", encoding="utf-8"
        )
        swapped = nominate_prompt(batch, "PCSK9")
        judge = swapped.read_text(encoding="utf-8")
        lists = swapped.with_name("prompt4_lists.md").read_text(encoding="utf-8")
        assert swapped == batch / "nominate" / "prompt4.md"
        assert "the target" in judge and "2018-06-01" in judge and "Kv1.3" not in judge
        assert "<<<CLOSED_LISTS>>>" in judge and "<<<AS_OF>>>" not in judge
        assert "<<<BIOLOGICAL_CONTEXT>>>" in judge and "plasmablasts" not in judge
        assert "PCSK9" in lists and "2018-06-01" in lists and "Kv1.3" not in lists
        assert "<<<BIOLOGICAL_CONTEXT>>>" in lists and "plasmablasts" not in lists
        assert "<<<AS_OF>>>" in (ROOT / "nominate" / "prompt4.md").read_text(encoding="utf-8")
        assert "Kv1.3" in (ROOT / "nominate" / "prompt4.md").read_text(encoding="utf-8")
    prior = ROOT / "nominate" / "prompt4_prior.md"
    body = prior.read_text(encoding="utf-8")
    novel = (ROOT / "nominate" / "prompt4.md").read_text(encoding="utf-8")
    shared = "whose ongoing injury follows a listed action"
    assert shared in body and shared in novel
    assert "Known indications." not in body
    assert "already a known indication" not in body
    assert "keep 10 to 15 indications" in body
    assert "is not already a known indication" in novel
    assert "still stays when it is not a known indication of the target" in novel
    assert "Do not require an existing direct" not in body
    assert "Do not require an existing direct" in novel
    lists = (ROOT / "nominate" / "prompt4_lists.md").read_text(encoding="utf-8")
    assert "Do not write disease names" in lists
    assert "known indication" not in lists
    assert "Each class of an immunoglobulin is its own product." not in lists
    assert "If a released class has isotypes, write each isotype as its own product." in lists
    assert "Disease class." in body and "Disease class." in novel
    with tempfile.TemporaryDirectory() as tmp:
        batch = Path(tmp)
        (batch / "batch.json").write_text(
            json.dumps({"target": "KCNA3", "as_of": "2026-04-30"}) + "\n", encoding="utf-8"
        )
        filled = nominate_prompt(batch, "KCNA3", prior)
        assert filled == batch / "nominate" / "prompt4.md"
        assert "2026-04-30" in filled.read_text(encoding="utf-8")
        assert "<<<BIOLOGICAL_CONTEXT>>>" not in filled.read_text(encoding="utf-8")
        assert bind_nominate_prompt(batch, prior.name) == prior
        assert bind_nominate_prompt(batch, "prompt4.md") == prior
    with tempfile.TemporaryDirectory() as tmp:
        batch = Path(tmp)
        (batch / "batch.json").write_text(
            json.dumps({"target": "PCSK9", "as_of": "2018-06-01"}) + "\n", encoding="utf-8"
        )
        swapped = nominate_prompt(batch, "PCSK9", prior)
        text = swapped.read_text(encoding="utf-8")
        assert swapped == batch / "nominate" / "prompt4.md"
        assert "already a known indication" not in text
        assert "Disease class." in text
        assert "the target" in text and "2018-06-01" in text and "Kv1.3" not in text
        assert _context_pending(swapped, batch)
    print("self_check ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        _self_check()
    else:
        main()
