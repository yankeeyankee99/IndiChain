"""Merge five nomination runs into nominate/counts.md.

Spellings are ordered by Part A, then Part B, then Part C, then first appearance.
Each spelling is resolved to a MeSH id in that order. The same id folds into the
earlier row and does not move it. A spelling with no id leaves a gap. The next
spelling fills that gap. Later rows stay in the table so a dropped place can be filled.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from batch_top5 import parse_counts_md

ROOT = Path(__file__).resolve().parent
HEADER = (
    "| # | Indication (merged name) | Files appearing in Part A | "
    "Files appearing in Part B | Files naming it in Part C | MeSH |"
)


def _section(text: str, start: str, end: str) -> str:
    low = text.lower()
    i = low.find(start.lower())
    if i < 0:
        return ""
    j = low.find(end.lower(), i + len(start))
    if j < 0:
        j = len(text)
    return text[i:j]


_INDICATION = re.compile(r"indicat|disease", re.I)
_BOLD_ITEM = re.compile(r"^\s*\d+\.\s+\*\*(.+?)\*\*")


def _clean_name(cell: str) -> str:
    name = cell.strip().replace("**", "")
    name = re.sub(r"^\*+|\*+$", "", name).strip()
    name = re.sub(r"^\d+[\.\)]\s*", "", name).strip()
    return name


def _name_key(name: str) -> str:
    text = name.casefold().replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


def part_a_names(text: str) -> list[str]:
    """Indication names from the first table, or from a numbered bold list."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("|")), None)
    names: list[str] = []
    if start is not None:
        rows: list[str] = []
        for line in lines[start:]:
            if not line.startswith("|"):
                break
            rows.append(line)
        if len(rows) >= 3:
            headers = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
            col = next((i for i, header in enumerate(headers) if _INDICATION.search(header)), 0)
            for line in rows[2:]:
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                if not cells or set(cells[0]) <= set("-: "):
                    continue
                raw = cells[col] if col < len(cells) else cells[0]
                name = _clean_name(raw)
                if name and not name.isdigit():
                    names.append(name)
    if names:
        return names
    for line in lines:
        match = _BOLD_ITEM.search(line)
        if match:
            name = _clean_name(match.group(1))
            if name:
                names.append(name)
    return names


_MESH_CELL = re.compile(r"D\d+")


def disease_mesh_rows(text: str) -> list[tuple[str, str]] | None:
    """Disease and MeSH cells. None when this is not that table."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("|")), None)
    if start is None:
        return None
    rows: list[str] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    if len(rows) < 2:
        return None
    headers = [cell.strip().casefold() for cell in rows[0].strip().strip("|").split("|")]
    if "mesh" not in headers or not any(_INDICATION.search(header) for header in headers):
        return None
    name_at = next(i for i, header in enumerate(headers) if _INDICATION.search(header))
    mesh_at = headers.index("mesh")
    found: list[tuple[str, str]] = []
    for line in rows[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: "):
            continue
        name = _clean_name(cells[name_at] if name_at < len(cells) else "")
        raw_mesh = cells[mesh_at] if mesh_at < len(cells) else ""
        mesh = raw_mesh if _MESH_CELL.fullmatch(raw_mesh) else ""
        if name and not name.isdigit():
            found.append((name, mesh))
    return found


def _mentions(haystack: str, name: str) -> bool:
    if not haystack or not name:
        return False
    key = name.casefold()
    if key in haystack:
        return True
    short = re.sub(r"\([^)]*\)", " ", key)
    short = re.sub(r"\[[^\]]*\]", " ", short)
    short = " ".join(short.replace("/", " ").split())
    if len(short) >= 4 and short in haystack:
        return True
    # First two content tokens often survive synonym edits in Part B/C.
    words = [w for w in re.split(r"[\s/,;:]+", short) if len(w) > 2]
    if len(words) >= 2 and " ".join(words[:2]) in haystack:
        return True
    return False


def indications(run_n: int, text: str) -> list[dict]:
    names = part_a_names(text)
    part_b = _section(text, "Part B", "Part C").casefold()
    part_c = _section(text, "Part C", "\0").casefold()
    rows = []
    for order, name in enumerate(names):
        rows.append(
            {
                "run": run_n,
                "name": name,
                "part_b": _mentions(part_b, name),
                "part_c": _mentions(part_c, name),
                "order": order,
            }
        )
    return rows


def find_answer(folder: Path, run_n: int) -> Path:
    if not folder.is_dir():
        raise SystemExit(f"merge failed: missing nomination folder {folder}")
    preferred = sorted(
        p
        for p in folder.glob(f"*_run{run_n}.md")
        if "_lists" not in p.name
        and "reasoning" not in p.name.casefold()
        and "thinking" not in p.name.casefold()
    )
    if preferred:
        return preferred[0]
    if (folder / "list.txt").is_file():
        return folder / "list.txt"
    md = [
        p
        for p in folder.glob("*.md")
        if "reasoning" not in p.name.casefold() and "thinking" not in p.name.casefold() and "_lists" not in p.name
    ]
    if len(md) == 1:
        return md[0]
    raise SystemExit(
        f"merge failed: {folder} needs one nomination answer "
        f"(*_run{run_n}.md or list.txt)."
    )


def runs_root(batch: Path) -> Path:
    if (batch / "nominate" / "run-1").is_dir():
        return batch / "nominate"
    if (batch / "run-1").is_dir():
        return batch
    return batch / "nominate"


def load_runs(batch: Path) -> list[tuple[int, Path, str]]:
    found = []
    missing = []
    root = runs_root(batch)
    for n in range(1, 6):
        folder = root / f"run-{n}"
        try:
            path = find_answer(folder, n)
        except SystemExit:
            missing.append(n)
            continue
        found.append((n, path, path.read_text(encoding="utf-8", errors="replace")))
    if missing:
        raise SystemExit(
            "merge failed: nomination runs incomplete. "
            f"Missing run folders or answers for: {missing}. "
            "Need nominate/run-1 … run-5 before merge."
        )
    return found


def resolve_name(name: str, lookup, pick) -> dict:
    """Four tries on a transport error. Zero records or a choice outside the return is no id."""
    error = ""
    saw_records = False
    for attempt in range(4):
        try:
            records, _raw = lookup(name)
        except Exception as exc:  # noqa: BLE001 — retry, then stop the merge
            error = str(exc).splitlines()[0][:300]
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
            continue
        if not records:
            return {"status": "no_match"}
        saw_records = True
        if len(records) == 1:
            return {"status": "matched", "mesh": records[0]["id"], "label": records[0]["label"]}
        try:
            reply = pick(name, records)
        except Exception as exc:  # noqa: BLE001 — a dead picker skips this name
            error = str(exc).splitlines()[0][:300]
            continue
        from batch_top5 import _choice_id

        mesh = _choice_id(reply, records)
        if not mesh:
            return {"status": "unmatched_choice"}
        label = next(row["label"] for row in records if row["id"] == mesh)
        return {"status": "matched", "mesh": mesh, "label": label}
    if saw_records:
        return {"status": "unmatched_choice"}
    raise SystemExit(f"merge failed: MeSH lookup failed for {name}: {error}")


def _display(label: str, aliases: list[str]) -> str:
    extra = []
    for name in aliases:
        if name.casefold() not in {label.casefold(), *(item.casefold() for item in extra)}:
            extra.append(name)
    if not extra:
        return label
    return f"{label} ({'; '.join(extra)})"


def count_rows(
    runs: list[tuple[int, Path, str]], lookup, pick, cache_path: Path | None = None
) -> tuple[list[dict], list[dict]]:
    items = [item for n, _path, text in runs for item in indications(n, text)]
    by_name: dict[str, dict] = {}
    for item in items:
        key = _name_key(item["name"])
        slot = by_name.get(key)
        if slot is None:
            slot = {
                "name": item["name"],
                "runs": set(),
                "part_b": set(),
                "part_c": set(),
                "first": (item["run"], item["order"]),
            }
            by_name[key] = slot
        slot["runs"].add(item["run"])
        if item["part_b"]:
            slot["part_b"].add(item["run"])
        if item["part_c"]:
            slot["part_c"].add(item["run"])
        slot["first"] = min(slot["first"], (item["run"], item["order"]))
    ordered = sorted(
        by_name.values(),
        key=lambda row: (-len(row["runs"]), -len(row["part_b"]), -len(row["part_c"]), row["first"]),
    )
    cache: dict[str, dict] = {}
    if cache_path is not None and cache_path.is_file():
        cache.update(json.loads(cache_path.read_text(encoding="utf-8")))
    skipped: list[dict] = []
    groups: dict[str, dict] = {}
    sequence: list[str] = []
    for slot in ordered:
        key = _name_key(slot["name"])
        if key not in cache:
            cache[key] = resolve_name(slot["name"], lookup, pick)
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        found = cache[key]
        if found["status"] == "matched":
            group_key = found["mesh"]
            label = found["label"]
            mesh_id = found["mesh"]
        else:
            skipped.append({"name": slot["name"], "status": found["status"]})
            group_key = "raw:" + key
            label = slot["name"]
            mesh_id = ""
        group = groups.get(group_key)
        if group is None:
            group = {
                "mesh": mesh_id,
                "label": label,
                "aliases": [],
                "runs": set(),
                "part_b": set(),
                "part_c": set(),
            }
            groups[group_key] = group
            sequence.append(group_key)
        else:
            skipped.append({"name": slot["name"], "status": "merged", "mesh": mesh_id})
        group["aliases"].append(slot["name"])
        group["runs"].update(slot["runs"])
        group["part_b"].update(slot["part_b"])
        group["part_c"].update(slot["part_c"])
    table = []
    for index, group_key in enumerate(sequence, 1):
        row = groups[group_key]
        table.append(
            {
                "rank": index,
                "name": _display(row["label"], row["aliases"]),
                "part_a": len(row["runs"]),
                "part_b": len(row["part_b"]),
                "part_c": len(row["part_c"]),
                "mesh": row["mesh"],
            }
        )
    table.sort(key=lambda row: (-row["part_a"], -row["part_b"], -row["part_c"], row["rank"]))
    for index, row in enumerate(table, 1):
        row["rank"] = index
    return table, skipped


def render_counts_md(rows: list[dict], source_note: str) -> str:
    lines = [
        "# Merged indication counts (five nomination runs)",
        "",
        "Counts are the number of runs, out of five, in which the merged indication appears.",
        f"Source pack: {source_note}",
        "",
        HEADER,
        "|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['rank']} | {row['name']} | {row['part_a']} | {row['part_b']} | {row['part_c']} | {row['mesh']} |"
        )
    return "\n".join(lines) + "\n"


def write_frequency_docx(folder: Path, rows: list[dict]) -> Path:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    def font(run, size: int, bold: bool = False) -> None:
        run.bold = bold
        run.font.size = Pt(size)
        run.font.name = "Calibri"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")

    def write_cell(cell, text: str, bold: bool = False) -> None:
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        font(run, 10, bold)

    doc = Document()
    title = doc.add_paragraph()
    title_run = title.add_run("Five-run summary")
    font(title_run, 16, True)
    note = doc.add_paragraph()
    note_run = note.add_run("Rows are merged by MeSH descriptor. The name is the preferred term. A row without an id keeps the model's wording.")
    font(note_run, 10)
    table = doc.add_table(rows=1 + len(rows), cols=4)
    table.style = "Table Grid"
    for index, header in enumerate(["Count", "MeSH", "Name", "Wording"]):
        write_cell(table.rows[0].cells[index], header, True)
    for index, row in enumerate(rows, 1):
        aliases = row["name"]
        label = aliases.split(" (", 1)[0]
        write_cell(table.rows[index].cells[0], f"{row['part_a']}/5")
        write_cell(table.rows[index].cells[1], row["mesh"])
        write_cell(table.rows[index].cells[2], label)
        write_cell(table.rows[index].cells[3], aliases)
    path = folder / "nomination-frequency.docx"
    doc.save(path)
    return path


def write_top5_md(folder: Path, rows: list[dict]) -> Path:
    picked = [row for row in rows if row["mesh"]][:5]
    lines = ["# top5", ""]
    for index, row in enumerate(picked, 1):
        lines.append(f"{index}. {row['name'].split(' (', 1)[0]} ({row['mesh']}) {row['part_a']}/5")
    path = folder / "top5.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _verified_mesh(name: str) -> tuple[str, str]:
    from run_agent_plan import mesh_lookup

    found = mesh_lookup(name)
    descriptors = found.get("descriptors") or []
    if found.get("error") or len(descriptors) != 1:
        return "", name
    row = descriptors[0]
    return str(row.get("mesh") or ""), str(row.get("preferred_label") or name)


def write_mesh_frequency(batch: Path, verify=None) -> Path:
    """Frequency table from Disease/MeSH answers, plus the top5 chain reads."""
    from batch_top5 import write_top5

    check = verify or _verified_mesh
    runs = load_runs(batch)
    nominate = runs_root(batch)
    cache: dict[str, tuple[str, str]] = {}
    groups: dict[str, dict] = {}
    for run_n, _path, text in runs:
        seen: set[str] = set()
        for name, _mesh in disease_mesh_rows(text) or []:
            key_name = " ".join(name.casefold().split())
            if key_name not in cache:
                cache[key_name] = check(name)
            mesh, label = cache[key_name]
            key = mesh or ("raw:" + key_name)
            if key in seen:
                continue
            seen.add(key)
            group = groups.get(key)
            if group is None:
                group = {"mesh": mesh, "name": label if mesh else name, "runs": set(), "aliases": []}
                groups[key] = group
            group["runs"].add(run_n)
            if name.casefold() not in {group["name"].casefold(), *(item.casefold() for item in group["aliases"])}:
                group["aliases"].append(name)
    ranked = sorted(groups.values(), key=lambda row: (-len(row["runs"]), row["name"].casefold()))
    total = len(runs)
    count_lines = [
        "# Merged indication counts",
        "",
        f"Counts are the number of runs, out of {total}, in which the disease appears.",
        "Sorted by that count, high to low. The MeSH id is kept when the disease name is that descriptor's heading or entry term.",
        "",
        HEADER,
        "|---|---|---:|---:|---:|---|",
    ]
    freq_lines = [
        "# Frequency",
        "",
        f"The denominator of the counts is {total}. Ordered from highest count to lowest.",
        "",
        "| Count | Disease | MeSH |",
        "|---:|---|---|",
    ]
    for index, row in enumerate(ranked, 1):
        count = len(row["runs"])
        count_lines.append(
            f"| {index} | {row['name']} | {count} | 0 | 0 | {row['mesh']} |"
        )
        freq_lines.append(f"| {count}/{total} | {row['name']} | {row['mesh']} |")
    nominate.mkdir(parents=True, exist_ok=True)
    out = nominate / "counts.md"
    out.write_text("\n".join(count_lines) + "\n", encoding="utf-8")
    (nominate / "frequency.md").write_text("\n".join(freq_lines) + "\n", encoding="utf-8")
    picked = [row for row in ranked if row["mesh"]][:5]
    top_lines = ["# top5", ""]
    for index, row in enumerate(picked, 1):
        top_lines.append(f"{index}. {row['name']} ({row['mesh']}) {len(row['runs'])}/{total}")
    (nominate / "top5.md").write_text("\n".join(top_lines) + "\n", encoding="utf-8")
    chain_batch = batch if (batch / "nominate").is_dir() else nominate.parent
    payload = write_top5(chain_batch, counts_path=out)
    payload["candidates"] = [row for row in payload["candidates"] if row.get("mesh")]
    (chain_batch / "nominate" / "top5.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out


def write_merge(batch: Path, *, lookup=None, pick=None) -> Path:
    from batch_top5 import _model_pick, lookup_mesh_heading

    runs = load_runs(batch)
    if runs and all(disease_mesh_rows(text) is not None for _n, _path, text in runs):
        return write_mesh_frequency(batch, verify=lookup if lookup is not None else None)
    nominate = runs_root(batch)
    cache_path = nominate / "mesh_lookup_cache.json"
    table, skipped = count_rows(runs, lookup or lookup_mesh_heading, pick or _model_pick, cache_path)
    if not table:
        raise SystemExit("merge failed: no Part A name resolved to a MeSH id")
    nominate = runs_root(batch)
    nominate.mkdir(parents=True, exist_ok=True)
    note = ", ".join(f"run-{n}" for n, _p, _t in runs)
    out = nominate / "counts.md"
    out.write_text(render_counts_md(table, note), encoding="utf-8")
    (nominate / "mesh_merge.json").write_text(
        json.dumps({"rows": table, "skipped": skipped}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_frequency_docx(nominate, table)
    write_top5_md(nominate, table)
    return out


def _self_check() -> None:
    import tempfile

    sample = """## Part A — Candidate list
| Disease indication and relevant subtype, with useful English synonyms | x |
|---|---|
| IgA nephropathy (Berger disease) | a |
| Berger disease | a |
| Giant cell arteritis | a |
| Not a disease | a |

## Part B — Candidate hypothesis cards
IgA nephropathy appears here.

## Part C — Handoff
Giant cell arteritis and IgA nephropathy.
"""
    ranked = """| # | Indication | x |
|---|---|---|
| 1 | Sjögren's syndrome | a |
"""
    assert part_a_names(ranked) == ["Sjögren's syndrome"]
    assert part_a_names("1. **Celiac disease** — *T cell activation*\n") == ["Celiac disease"]
    numbered = """| Indication | x |
|---|---|
| 1. Graft-versus-host disease | a |
"""
    assert part_a_names(numbered) == ["Graft-versus-host disease"]
    names = part_a_names(sample)
    assert names[0].startswith("IgA nephropathy"), names
    packed = indications(1, sample)
    assert packed[0]["part_b"] and packed[0]["part_c"]
    assert packed[2]["name"] == "Giant cell arteritis"

    def lookup(name: str):
        if name == "Flaky":
            raise TimeoutError("down")
        if "Not a disease" in name:
            return [], {}
        if "Berger" in name or "IgA" in name:
            return [{"id": "D005922", "label": "Glomerulonephritis, IGA"}], {}
        if "Giant" in name:
            return [
                {"id": "D013700", "label": "Giant Cell Arteritis"},
                {"id": "D001234", "label": "Other"},
            ], {}
        if name == "Sarcoidosis":
            return [{"id": "D012507", "label": "Sarcoidosis"}], {}
        return [], {}

    def pick(name: str, records: list[dict]) -> str:
        assert "Giant" in name
        return "D013700"

    with tempfile.TemporaryDirectory() as tmp:
        batch = Path(tmp)
        for n in range(1, 6):
            folder = batch / "nominate" / f"run-{n}"
            folder.mkdir(parents=True)
            text = sample
            if n == 5:
                text = sample.replace("IgA nephropathy (Berger disease)", "Sarcoidosis").replace(
                    "IgA nephropathy", "Sarcoidosis"
                )
            (folder / f"K-EN_deepseek-v4-pro_run{n}.md").write_text(text, encoding="utf-8")
        out = write_merge(batch, lookup=lookup, pick=pick)
        rows = parse_counts_md(out.read_text(encoding="utf-8"))
        assert [row["mesh"] for row in rows] == ["D005922", "D013700", "", "D012507"], rows
        assert rows[0]["part_a"] == 5 and rows[0]["part_b"] == 4 and rows[0]["part_c"] == 4
        assert rows[1]["part_a"] == 5 and rows[1]["part_b"] == 0 and rows[1]["part_c"] == 5
        assert rows[2]["name"] == "Not a disease" and rows[2]["part_a"] == 5
        assert rows[3]["part_a"] == 1
        assert (batch / "nominate" / "nomination-frequency.docx").is_file()
        assert (batch / "nominate" / "top5.md").is_file()
        skipped = json.loads((batch / "nominate" / "mesh_merge.json").read_text(encoding="utf-8"))["skipped"]
        assert any(row["status"] == "no_match" for row in skipped)
        assert any(row["status"] == "merged" and row["mesh"] == "D005922" for row in skipped)

        flaky = batch / "nominate" / "run-1" / "K-EN_deepseek-v4-pro_run1.md"
        flaky.write_text(sample.replace("Not a disease", "Flaky"), encoding="utf-8")
        tries = {"n": 0}

        def flaky_lookup(name: str):
            if name == "Flaky":
                tries["n"] += 1
                if tries["n"] == 1:
                    raise TimeoutError("down")
                return [{"id": "D000001", "label": "Flaky disease"}], {}
            return lookup(name)

        write_merge(batch, lookup=flaky_lookup, pick=pick)
        assert tries["n"] == 2
        cache = batch / "nominate" / "mesh_lookup_cache.json"
        if cache.is_file():
            cache.unlink()
        try:
            write_merge(batch, lookup=lambda name: (_ for _ in ()).throw(TimeoutError("down")), pick=pick)
            raise AssertionError("lookup failure must stop the merge")
        except SystemExit as exc:
            assert "lookup failed" in str(exc)

    mesh_table = """| Disease | MeSH |
|---|---|
| Glomerulonephritis, IGA | D005922 |
| Psoriasis | D011565 |
| Made up disease | D000922 |
"""
    with tempfile.TemporaryDirectory() as tmp:
        batch = Path(tmp)
        for n in range(1, 6):
            folder = batch / "nominate" / f"run-{n}"
            folder.mkdir(parents=True)
            text = mesh_table
            if n > 3:
                text = mesh_table.replace("| Psoriasis | D011565 |\n", "")
            (folder / f"K_run{n}.md").write_text(text, encoding="utf-8")

        def verify(name: str) -> tuple[str, str]:
            if name == "Glomerulonephritis, IGA":
                return "D005922", "Glomerulonephritis, IGA"
            if name == "Psoriasis":
                return "D011565", "Psoriasis"
            return "", name

        out = write_mesh_frequency(batch, verify=verify)
        rows = parse_counts_md(out.read_text(encoding="utf-8"))
        assert [row["mesh"] for row in rows] == ["D005922", "", "D011565"], rows
        assert rows[0]["part_a"] == 5 and rows[2]["part_a"] == 3
        freq = (batch / "nominate" / "frequency.md").read_text(encoding="utf-8")
        assert freq.index("5/5") < freq.index("3/5")
        top5 = json.loads((batch / "nominate" / "top5.json").read_text(encoding="utf-8"))
        assert top5["candidates"][0]["mesh"] == "D005922"
        assert "Psoriasis (D011565) 3/5" in (batch / "nominate" / "top5.md").read_text(encoding="utf-8")

    print("self_check_ok")


if __name__ == "__main__":
    import sys

    if "--self-check" in sys.argv:
        _self_check()
