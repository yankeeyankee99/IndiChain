"""Nominate each freeze date in the summary document.

One biological context is generated per gene symbol and reused for every date of that symbol.
Each date gets its own mechanism lists and five prompt4 runs. The indication printed beside a date is not put into the prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import merge_counts
import run_agent_plan

ROOT = Path(__file__).resolve().parent
DOCX = ROOT.parent / "nominate结果" / "11.2.15.53获批冻结" / "提名汇总.docx"
OUT = ROOT / "data" / "catalog"
MODEL = "deepseek-v4-pro"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

EXPECTED = [
    ("SLC5A2", "2017-03-01"),
    ("IL17A", "2016-07-11"),
    ("CD19", "2016-03-01"),
    ("IL4R", "2015-05-12"),
    ("IL4R", "2019-04-15"),
    ("IL4R", "2020-10-28"),
    ("IL4R", "2020-12-01"),
    ("C3", "2015-09-24"),
    ("CFB", "2018-02-07"),
    ("FCGRT", "2019-03-26"),
    ("C5", "2008-10-01"),
    ("C5", "2009-04-01"),
    ("IL23A", "2015-03-27"),
    ("IL5", "2012-01-01"),
    ("IL5RA", "2017-04-15"),
    ("TSLP", "2021-04-22"),
]


def docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    rows = []
    for para in root.iter(f"{W}p"):
        text = "".join(node.text or "" for node in para.iter(f"{W}t")).strip()
        if text:
            rows.append(text)
    return rows


def _symbol(line: str) -> str:
    if not line.strip():
        return ""
    token = line.split()[0]
    if len(token) < 2 or len(token) > 12:
        return ""
    if not token[0].isupper() or not token.isalnum() or not token.isupper():
        return ""
    if token.startswith("D") and token[1:].isdigit():
        return ""
    return token


def parse_catalog(path: Path) -> list[tuple[str, str]]:
    symbol = ""
    found: list[tuple[str, str]] = []
    for line in docx_paragraphs(path):
        if len(line) >= 10 and line[4] == "-" and line[7] == "-" and line[:10].replace("-", "").isdigit():
            if not symbol:
                raise SystemExit(f"freeze date before a target: {line}")
            found.append((symbol, line[:10]))
            continue
        name = _symbol(line)
        if name:
            symbol = name
    return found


def _swap(text: str, symbol: str, context: str, end_marker: str) -> str:
    marker = "Biological context\n"
    start = text.find(marker)
    end = text.find(end_marker)
    if start < 0 or end < start:
        raise SystemExit("biological context block not found")
    head = text[:start].replace("Kv1.3", symbol)
    tail = text[end:].replace("Kv1.3", symbol)
    return head + marker + context.strip() + "\n" + tail


def _run(prompt: Path, out: Path, run: int, tag: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "run_agent_plan.py"),
        MODEL,
        "--thinking",
        "enabled",
        "--run",
        str(run),
        "--tag",
        tag,
        "--prompt",
        str(prompt),
        "--out",
        str(out),
    ]
    for attempt in range(2):
        for path in out.glob(f"*{tag}*run{run}*"):
            if path.is_file():
                path.unlink()
        completed = subprocess.run(cmd, cwd=ROOT)
        if completed.returncode == 0:
            return
        if attempt == 0:
            print(f"retry {tag} {out} run {run}", flush=True)
    raise SystemExit(f"run failed: {tag} {out} run {run}")


def _answer_ready(folder: Path, run: int) -> bool:
    files = [
        path
        for path in folder.glob(f"*_run{run}.md")
        if "reasoning" not in path.name.casefold() and "_lists" not in path.name
    ]
    return bool(files) and "|" in files[0].read_text(encoding="utf-8")


def _ensure_context(symbol: str) -> str:
    folder = OUT / symbol
    folder.mkdir(parents=True, exist_ok=True)
    saved = folder / "context.md"
    if saved.is_file() and saved.read_text(encoding="utf-8").strip():
        return saved.read_text(encoding="utf-8").strip()
    ask = (ROOT / "prompt_context.md").read_text(encoding="utf-8").replace("<<<TARGET>>>", symbol)
    prompt = folder / "context-prompt.md"
    prompt.write_text(ask, encoding="utf-8")
    _run(prompt, folder / "context-run", 1, "CONTEXT")
    text = (folder / "context-run" / "CONTEXT_deepseek-v4-pro_run1.md").read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"empty biological context for {symbol}")
    saved.write_text(text + "\n", encoding="utf-8")
    return text


def _ensure_lists(symbol: str, as_of: str, context: str) -> str:
    job = OUT / symbol / as_of
    saved = job / "lists.md"
    if saved.is_file() and "## Products" in saved.read_text(encoding="utf-8"):
        return saved.read_text(encoding="utf-8").strip()
    raw = (ROOT / "prompt4_lists.md").read_text(encoding="utf-8")
    filled = _swap(raw, symbol, context, "\nUse these headings").replace("<<<AS_OF>>>", as_of)
    prompt = job / "lists-prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text(filled, encoding="utf-8")
    _run(prompt, job / "lists-run", 1, "LISTS")
    content = (job / "lists-run" / "LISTS_deepseek-v4-pro_run1.md").read_text(encoding="utf-8")
    lists = run_agent_plan.extract_lists(content) or ""
    if not lists:
        raise SystemExit(f"closed lists not found for {symbol} {as_of}")
    saved.write_text(lists + "\n", encoding="utf-8")
    return lists


def _nominate(symbol: str, as_of: str, context: str, lists: str) -> None:
    job = OUT / symbol / as_of
    (job / "batch.json").write_text(
        json.dumps({"target": symbol, "as_of": as_of}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    raw = (ROOT / "prompt4.md").read_text(encoding="utf-8")
    filled = _swap(raw, symbol, context, "\nClosed mechanism lists").replace("<<<AS_OF>>>", as_of)
    filled = filled.replace("<<<CLOSED_LISTS>>>", lists)
    prompt = job / "prompt4.md"
    prompt.write_text(filled, encoding="utf-8")
    pending = []
    for n in range(1, 6):
        dest = job / "nominate" / f"run-{n}"
        if not _answer_ready(dest, n):
            pending.append((n, dest))
    if pending:
        procs = []
        for n, dest in pending:
            dest.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                "-u",
                str(ROOT / "run_agent_plan.py"),
                MODEL,
                "--thinking",
                "enabled",
                "--run",
                str(n),
                "--tag",
                "PROMPT4",
                "--prompt",
                str(prompt),
                "--out",
                str(dest),
            ]
            procs.append((n, subprocess.Popen(cmd, cwd=ROOT)))
        failed = [n for n, proc in procs if proc.wait() != 0]
        for n in failed:
            print(f"retry PROMPT4 {symbol} {as_of} run {n}", flush=True)
            _run(prompt, job / "nominate" / f"run-{n}", n, "PROMPT4")
    for n in range(1, 6):
        if not _answer_ready(job / "nominate" / f"run-{n}", n):
            raise SystemExit(f"missing answer {symbol} {as_of} run {n}")
    merge_counts.write_merge(job)
    print(f"merged {symbol} {as_of}", flush=True)


def _finished(symbol: str, as_of: str) -> bool:
    return (OUT / symbol / as_of / "nominate" / "frequency.md").is_file()


# MeSH ids already attached to these indications. A row is marked when its id is in this set.
# Eosinophilic COPD is the broader COPD descriptor. Nasal polyps and rhinosinusitis are the two
# descriptors used for chronic rhinosinusitis with nasal polyps.
TARGET_MESH = {
    ("SLC5A2", "2017-03-01"): {"D054144"},
    ("IL17A", "2016-07-11"): {"D017497"},
    ("CD19", "2016-03-01"): {"D000077733"},
    ("IL4R", "2015-05-12"): {"D057765"},
    ("IL4R", "2019-04-15"): {"D029424"},
    ("IL4R", "2020-10-28"): {"D010391"},
    ("IL4R", "2020-12-01"): {"D000092562"},
    ("C3", "2015-09-24"): {"D057092"},
    ("CFB", "2018-02-07"): {"D005922"},
    ("FCGRT", "2019-03-26"): {"D020277"},
    ("C5", "2008-10-01"): {"D009157"},
    ("C5", "2009-04-01"): {"D009471"},
    ("IL23A", "2015-03-27"): {"D015535"},
    ("IL5", "2012-01-01"): {"D029424"},
    ("IL5RA", "2017-04-15"): {"D015267"},
    ("TSLP", "2021-04-22"): {"D009298", "D000096825"},
}

OLD_TABLE_NOTE = "The tables below are still the nominations from the freeze date before this change, and have not been rerun on the new date."
NEW_TABLE_NOTE = "The tables below are the five nominations rerun on these freeze dates."


def _flag(name: str) -> str | None:
    if name not in sys.argv:
        return None
    index = sys.argv.index(name)
    if index + 1 >= len(sys.argv):
        raise SystemExit(f"missing value for {name}")
    return sys.argv[index + 1]


def _freq_rows(path: Path) -> list[tuple[str, str, str]] | None:
    if not path.is_file():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) >= 3 and "/" in parts[0]:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _paint(cell, text: str, yellow: bool) -> None:
    from docx.enum.text import WD_COLOR_INDEX
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    cell.text = ""
    run = cell.paragraphs[0].add_run(text)
    run.font.name = "Calibri"
    run.font.size = Pt(10)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    tc_pr = cell._tc.get_or_add_tcPr()
    for old in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(old)
    if yellow:
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        shade = OxmlElement("w:shd")
        shade.set(qn("w:val"), "clear")
        shade.set(qn("w:color"), "auto")
        shade.set(qn("w:fill"), "FFFF00")
        tc_pr.append(shade)


def write_summary(src: Path) -> Path:
    """Copy the freeze-date narrative and replace each frequency table with this run."""
    from docx import Document
    from docx.oxml.ns import qn

    dest = OUT / "nomination-summary.docx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    doc = Document(dest)
    for para in doc.paragraphs:
        if OLD_TABLE_NOTE not in para.text or not para.runs:
            continue
        para.runs[0].text = para.text.replace(OLD_TABLE_NOTE, NEW_TABLE_NOTE)
        for run in para.runs[1:]:
            run.text = ""
    symbol = ""
    dates: list[tuple[str, str]] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        name = _symbol(text)
        if name:
            symbol = name
        if len(text) >= 10 and text[4] == "-" and text[7] == "-" and text[:10].replace("-", "").isdigit():
            dates.append((symbol, text[:10]))
    if len(dates) != len(doc.tables):
        raise SystemExit(f"summary dates {len(dates)} tables {len(doc.tables)}")
    for (name, as_of), table in zip(dates, doc.tables):
        tbl = table._tbl
        for row in tbl.findall(qn("w:tr"))[1:]:
            tbl.remove(row)
        found = _freq_rows(OUT / name / as_of / "nominate" / "frequency.md")
        body = found if found else [("incomplete", "", "")]
        wanted = TARGET_MESH.get((name, as_of), set())
        for count, disease, mesh in body:
            cells = table.add_row().cells
            yellow = mesh in wanted
            _paint(cells[0], count, yellow)
            _paint(cells[1], disease, yellow)
            _paint(cells[2], mesh, yellow)
    doc.save(dest)
    done = sum(1 for name, as_of in dates if _finished(name, as_of))
    print(f"summary {dest} finished {done}/{len(dates)}", flush=True)
    return dest


def _enable_http_cache() -> None:
    root = OUT / "http-cache" / "v1"
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "format.json"
    if not marker.is_file():
        marker.write_bytes(
            (
                json.dumps(
                    {
                        "format": run_agent_plan.CACHE_FORMAT,
                        "version": run_agent_plan.CACHE_VERSION,
                        "mesh": "whitespace-normalized name, case preserved",
                        "clinical_trials": ["as_of", "condition", "intervention"],
                        "clinical_trials_cap": 15,
                        "clinical_trials_rule": "start < as_of",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        )
    os.environ[run_agent_plan.CACHE_ENV] = str(root)


def main() -> None:
    global OUT, DOCX
    only = None
    if "--only" in sys.argv:
        index = sys.argv.index("--only")
        only = (sys.argv[index + 1], sys.argv[index + 2])
    out_arg = _flag("--out")
    if out_arg:
        OUT = Path(out_arg)
        if not OUT.is_absolute():
            OUT = (ROOT / OUT).resolve()
    docx_arg = _flag("--docx")
    if docx_arg:
        DOCX = Path(docx_arg)
    if only:
        rows = [only]
    elif DOCX.is_file():
        rows = parse_catalog(DOCX)
    else:
        rows = list(EXPECTED)
    print(f"catalog {len(rows)} dates", flush=True)
    if "--check" in sys.argv:
        assert rows == EXPECTED, rows
        print("catalog_check_ok", len(rows))
        return
    _enable_http_cache()
    for symbol in dict.fromkeys(symbol for symbol, _date in rows):
        pending = [date for name, date in rows if name == symbol and not _finished(symbol, date)]
        if not pending:
            print(f"skip {symbol}", flush=True)
            continue
        try:
            context = _ensure_context(symbol)
        except SystemExit as exc:
            print(f"failed context {symbol}: {exc}", flush=True)
            continue
        for as_of in pending:
            try:
                lists = _ensure_lists(symbol, as_of, context)
                _nominate(symbol, as_of, context, lists)
            except SystemExit as exc:
                print(f"failed {symbol} {as_of}: {exc}", flush=True)
    if DOCX.is_file() and "--only" not in sys.argv:
        write_summary(DOCX)


if __name__ == "__main__":
    main()
