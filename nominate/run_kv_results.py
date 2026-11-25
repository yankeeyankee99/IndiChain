"""Four Kv1.3 nominate batches at freeze 2026-04-30, model deepseek-v4-pro-0813.

Each Results folder gets its own mechanism lists, five indication runs, and a merge.
Results1 is left as it is.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import merge_counts
import run_agent_plan

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "Kv1.3"
MODEL = run_agent_plan.GATEWAY_MODEL
AS_OF = "2026-04-30"
TARGET = "Kv1.3"


def _cmd(prompt: Path, out: Path, run: int, tag: str) -> list[str]:
    return [
        sys.executable, "-u", str(ROOT / "run_agent_plan.py"), MODEL,
        "--thinking", "enabled",
        "--run", str(run),
        "--tag", tag,
        "--prompt", str(prompt),
        "--out", str(out),
    ]


def _answer_ready(folder: Path, run: int) -> bool:
    files = [
        path
        for path in folder.glob(f"*_run{run}.md")
        if "reasoning" not in path.name.casefold() and "_lists" not in path.name
    ]
    return bool(files) and "|" in files[0].read_text(encoding="utf-8")


def _once(prompt: Path, out: Path, run: int, tag: str, log) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        for path in out.glob(f"*{tag}*run{run}*"):
            if path.is_file():
                path.unlink()
        completed = subprocess.run(_cmd(prompt, out, run, tag), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if completed.returncode == 0:
            return
        print(f"retry {tag} {out.name} run {run}", flush=True)
    raise SystemExit(f"run failed: {tag} {out} run {run}")


def _lists(job: Path, template: str, log) -> str:
    saved = job / "lists.md"
    if saved.is_file() and "## Products" in saved.read_text(encoding="utf-8"):
        return saved.read_text(encoding="utf-8").strip()
    prompt = job / "lists-prompt.md"
    prompt.write_text(template, encoding="utf-8")
    _once(prompt, job / "lists-run", 1, "LISTS", log)
    answers = [
        path for path in (job / "lists-run").glob("LISTS_*.md")
        if "reasoning" not in path.name
    ]
    if len(answers) != 1:
        raise SystemExit(f"lists answer missing in {job}")
    content = answers[0]
    lists = run_agent_plan.extract_lists(content.read_text(encoding="utf-8")) or ""
    if not lists:
        raise SystemExit(f"closed lists not found in {job}")
    saved.write_text(lists + "\n", encoding="utf-8")
    return lists


def _nominate(job: Path, template: str, lists: str, log) -> None:
    (job / "batch.json").write_text(
        json.dumps({"target": TARGET, "as_of": AS_OF, "model": MODEL}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    prompt = job / "prompt4.md"
    prompt.write_text(template.replace("<<<CLOSED_LISTS>>>", lists), encoding="utf-8")
    pending = []
    for n in range(1, 6):
        dest = job / "nominate" / f"run-{n}"
        if not _answer_ready(dest, n):
            pending.append((n, dest))
    procs = []
    for n, dest in pending:
        dest.mkdir(parents=True, exist_ok=True)
        procs.append((n, subprocess.Popen(_cmd(prompt, dest, n, "PROMPT4"), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)))
    failed = [n for n, proc in procs if proc.wait() != 0]
    for n in failed:
        print(f"retry PROMPT4 {job.name} run {n}", flush=True)
        _once(prompt, job / "nominate" / f"run-{n}", n, "PROMPT4", log)
    for n in range(1, 6):
        if not _answer_ready(job / "nominate" / f"run-{n}", n):
            raise SystemExit(f"missing answer {job.name} run {n}")
    merge_counts.write_merge(job)
    print(f"merged {job.name}", flush=True)


def main() -> None:
    lists_template = (ROOT / "prompt4_lists.md").read_text(encoding="utf-8").replace("<<<AS_OF>>>", AS_OF)
    judge_template = (ROOT / "prompt4.md").read_text(encoding="utf-8").replace("<<<AS_OF>>>", AS_OF)
    if "Kv1.3" not in lists_template or "<<<AS_OF>>>" in judge_template:
        raise SystemExit("prompt fill failed")
    for n in range(2, 6):
        job = DATA / f"Results{n}"
        job.mkdir(parents=True, exist_ok=True)
        counts = job / "nominate" / "counts.md"
        if counts.is_file() and _answer_ready(job / "nominate" / "run-5", 5):
            print(f"skip {job.name}", flush=True)
            continue
        with (job / "run.log").open("a", encoding="utf-8") as log:
            lists = _lists(job, lists_template, log)
            _nominate(job, judge_template, lists, log)
    print("kv_results_done", flush=True)


if __name__ == "__main__":
    main()
