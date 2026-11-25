"""Read a written count table into top5.json, then MeSH scopes.

Authority is the written merged count table path passed in. The file is read in
place and is not copied into the batch. Candidates keep table order. MeSH walks
that list until five distinct descriptors match, or the list ends. A merged or
failed row is skipped and the next row fills that place. Fewer than five
matches is allowed.

MeSH uses the id already written on a count-table row when that column is present.
Otherwise it uses the public descriptor lookup. One returned descriptor is used as written.
Several descriptors ask the model to pick an id that is already in that response.
Zero descriptors, a choice outside the response, or a MeSH id already taken skips
that row. Lookup stops once five distinct ids are matched. Later rows fill a skipped place.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAIN_SCOPE = ROOT / "chain" / "scope.yaml"
LOOKUP = "https://id.nlm.nih.gov/mesh/lookup/descriptor"
MESH_ID = re.compile(r"D\d+")
WANT = 5
# ponytail: contains lookup keeps 10 records. A later hit is invisible; raise the limit if a real disease is missing from the saved response.
LOOKUP_LIMIT = 10
ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|(?:\s*(D\d+)?\s*\|)?$"
)


def parse_counts_md(text: str) -> list[dict]:
    """Data rows of a five-run counts table, in the order written."""
    rows = []
    for line in text.splitlines():
        match = ROW.match(line.strip())
        if not match:
            continue
        rows.append(
            {
                "rank": int(match.group(1)),
                "name": match.group(2).strip(),
                "part_a": int(match.group(3)),
                "part_b": int(match.group(4)),
                "part_c": int(match.group(5)),
                "mesh": match.group(6) or "",
            }
        )
    return rows


def write_top5(batch: Path, counts_path: Path | None = None) -> dict:
    """Read the merged count table in place. Do not copy it into the batch."""
    if counts_path is None or not str(counts_path).strip():
        raise SystemExit(
            "top5 failed: no merged count table. Pass --counts <path>, or run "
            "--stage merge so nominate/counts.md exists. This stage does not "
            "rebuild counts from Part A nomination answers."
        )
    source = Path(counts_path)
    if not source.is_file():
        raise SystemExit(
            f"top5 failed: merged count table not found: {source}. "
            "Pass --counts to an existing file. This stage does not invent the table."
        )
    rows = parse_counts_md(source.read_text(encoding="utf-8"))
    if not rows:
        raise SystemExit(
            f"top5 failed: {source} has no data rows in the expected count-table format."
        )
    nominate = batch / "nominate"
    nominate.mkdir(parents=True, exist_ok=True)
    candidates = []
    for row in rows:
        item = {
            "rank": row["rank"],
            "name": row["name"],
            "part_a": row["part_a"],
            "part_b": row["part_b"],
            "part_c": row["part_c"],
        }
        if row.get("mesh"):
            item["mesh"] = row["mesh"]
        candidates.append(item)
    payload = {
        "source": str(source.resolve()),
        "want": WANT,
        "candidates": candidates,
        "diseases": [],
        "skipped": [],
    }
    (nominate / "top5.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def parse_lookup(payload: object) -> list[dict]:
    rows = payload if isinstance(payload, list) else []
    out = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        resource = str(row.get("resource") or "")
        label = str(row.get("label") or "").strip()
        tail = resource.rstrip("/").split("/")[-1]
        if not MESH_ID.fullmatch(tail) or tail in seen:
            continue
        seen.add(tail)
        out.append({"id": tail, "label": label or tail})
    return out


def query_variants(name: str) -> list[str]:
    """Display names from the count table are often not MeSH preferred labels."""
    text = " ".join(str(name or "").split()).strip()
    out: list[str] = []

    def add(value: str) -> None:
        value = " ".join(value.split()).strip(" ,;/")
        if value and value.casefold() not in {x.casefold() for x in out}:
            out.append(value)

    add(text)
    folded = _ascii_name(text)
    add(folded)
    add(re.sub(r"'s\b", "", folded, flags=re.I))
    add(re.sub(r"\([^)]*\)", " ", text))
    add(re.sub(r"\[[^\]]*\]", " ", text))
    for sep in ("/", ";", "—", " - "):
        if sep in text:
            add(text.split(sep, 1)[0])
    # First two content words catch labels like "Pemphigus vulgaris/foliaceus".
    words = [w for w in re.split(r"[\s/,;]+", out[0] if out else text) if w]
    if len(words) >= 2:
        add(" ".join(words[:2]))
    if words:
        add(words[0])
        add(words[-1])
    return out


def _ascii_name(text: str) -> str:
    text = str(text or "").replace("’", "'").replace("‘", "'").replace("`", "'")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split())


def _tokens(text: str) -> list[str]:
    folded = _ascii_name(text).casefold()
    folded = re.sub(r"'s\b", " ", folded).replace("'", "")
    return [word for word in re.split(r"[^a-z0-9]+", folded) if len(word) >= 4]


def _ranked(name: str, records: list[dict]) -> list[dict]:
    """Keep labels that share the query words. Prefer the label with the fewest extra words."""
    want = _tokens(name)
    if not records or not want:
        return records
    covered = []
    shared = []
    for row in records:
        have = set(_tokens(row["label"]))
        if all(word in have for word in want):
            covered.append((len(have - set(want)), row))
        elif have & set(want):
            shared.append(row)
    if not covered:
        return shared
    covered.sort(key=lambda item: item[0])
    best = covered[0][0]
    return [row for extra, row in covered if extra == best]


def _read_json(url: str, timeout: int) -> object:
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — MeSH drops the connection; try the same URL again
            last = exc
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    assert last is not None
    raise last


def _descriptor_lookup(label: str, match: str) -> list[dict]:
    query = urllib.parse.urlencode(
        {"label": label, "match": match, "limit": LOOKUP_LIMIT}
    )
    return parse_lookup(_read_json(f"{LOOKUP}?{query}", 30))


def _sparql_term(fragment: str) -> list[dict]:
    needle = fragment.casefold().replace('"', "")
    if len(needle) < 4:
        return []
    q = (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#>\n"
        "SELECT DISTINCT ?id ?label WHERE {\n"
        "  ?d meshv:identifier ?id .\n"
        "  ?d rdfs:label ?label .\n"
        "  ?d meshv:preferredConcept ?c .\n"
        "  ?c meshv:term ?t .\n"
        "  { ?t meshv:prefLabel ?term } UNION { ?t meshv:altLabel ?term }\n"
        f'  FILTER(CONTAINS(LCASE(STR(?term)), "{needle}"))\n'
        "} LIMIT 20"
    )
    url = "https://id.nlm.nih.gov/mesh/sparql?" + urllib.parse.urlencode(
        {"query": q, "format": "JSON", "inference": "false"}
    )
    data = _read_json(url, 60)
    out = []
    seen = set()
    for row in data.get("results", {}).get("bindings") or []:
        mesh = str((row.get("id") or {}).get("value") or "")
        label = str((row.get("label") or {}).get("value") or "").strip()
        if not MESH_ID.fullmatch(mesh) or mesh in seen:
            continue
        seen.add(mesh)
        out.append({"id": mesh, "label": label or mesh})
    return out


def _search_label(name: str, label: str, raw: dict) -> list[dict]:
    for match in ("exact", "contains"):
        body = _descriptor_lookup(label, match)
        raw["queries"].append({"label": label, "match": match, "n": len(body)})
        kept = _ranked(name, body)
        if kept:
            return kept
    body = _sparql_term(label)
    raw["queries"].append({"label": label, "match": "sparql_term", "n": len(body)})
    return _ranked(name, body)


def _id_prompt(name: str) -> str:
    return (
        f"Disease: {name}\n\n"
        "Reply with the MeSH descriptor unique ID for this disease and nothing else.\n"
    )


def mesh_id_for(name: str) -> str:
    from run_agent_plan import message_parts, post, request_body

    payload, error, _elapsed = post(
        request_body("deepseek-v4-pro", _id_prompt(name), 4000, False, "enabled", None),
        False,
    )
    if payload is None:
        raise RuntimeError(error or "mesh id failed")
    content, _reasoning, _finish = message_parts(payload)
    found = MESH_ID.findall(content or "")
    if len(found) == 1:
        return found[0]
    return ""


def _label_for_id(mesh_id: str) -> str:
    q = (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        f"SELECT ?label WHERE {{ <http://id.nlm.nih.gov/mesh/{mesh_id}> rdfs:label ?label }}"
    )
    url = "https://id.nlm.nih.gov/mesh/sparql?" + urllib.parse.urlencode(
        {"query": q, "format": "JSON", "inference": "false"}
    )
    data = _read_json(url, 60)
    for row in data.get("results", {}).get("bindings") or []:
        label = str((row.get("label") or {}).get("value") or "").strip()
        if label:
            return label
    return ""


def _terms_for_id(mesh_id: str) -> list[str]:
    q = (
        "PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#>\n"
        "SELECT DISTINCT ?label WHERE {\n"
        f"  <http://id.nlm.nih.gov/mesh/{mesh_id}> meshv:preferredConcept ?c .\n"
        "  ?c meshv:term ?t .\n"
        "  { ?t meshv:prefLabel ?label } UNION { ?t meshv:altLabel ?label }\n"
        "}"
    )
    url = "https://id.nlm.nih.gov/mesh/sparql?" + urllib.parse.urlencode(
        {"query": q, "format": "JSON", "inference": "false"}
    )
    data = _read_json(url, 60)
    out = []
    seen = set()
    for row in data.get("results", {}).get("bindings") or []:
        label = str((row.get("label") or {}).get("value") or "").strip()
        key = label.casefold()
        if label and key not in seen:
            seen.add(key)
            out.append(label)
    return out


def _same_disease(name: str, terms: list[str]) -> bool:
    key = " ".join(name.casefold().split())
    return any(" ".join(term.casefold().split()) == key for term in terms)


def lookup_mesh_heading(name: str) -> tuple[list[dict], dict]:
    """Ask for a MeSH id. Keep it only when the disease name is that descriptor's own heading or entry term."""
    mesh_id = mesh_id_for(name)
    raw: dict = {"queries": [], "id": mesh_id}
    if not mesh_id:
        return [], raw
    label = _label_for_id(mesh_id)
    terms = _terms_for_id(mesh_id)
    raw["queries"].append({"id": mesh_id, "label": label, "term_n": len(terms)})
    if not label or not _same_disease(name, [label, *terms]):
        return [], raw
    return [{"id": mesh_id, "label": label}], raw


def lookup_mesh(name: str) -> tuple[list[dict], dict]:
    raw: dict = {"queries": []}
    labels = query_variants(name)
    long = [label for label in labels if len(_tokens(label)) >= 2]
    short = [label for label in labels if label not in long]
    found: list[dict] = []
    for group in (long, short):
        for label in group:
            found = _search_label(name, label, raw)
            if found:
                return found, raw
    return found, raw


def _pick_prompt(name: str, records: list[dict]) -> str:
    lines = "\n".join(f"{row['id']}\t{row['label']}" for row in records)
    return (
        f"The nomination name is: {name}\n\n"
        "MeSH returned only these descriptor records. Choose the one record that is this disease.\n"
        "Reply with that descriptor id and nothing else. If none of the records is this disease, reply NONE.\n\n"
        f"{lines}\n"
    )


def _model_pick(name: str, records: list[dict]) -> str:
    from run_agent_plan import message_parts, post, request_body

    prompt = _pick_prompt(name, records)
    payload, error, _elapsed = post(
        request_body("deepseek-v4-pro", prompt, 2000, True, "enabled", None),
        True,
    )
    if payload is None:
        raise RuntimeError(error or "model pick failed")
    content, _reasoning, _finish = message_parts(payload)
    return content.strip()


def _choice_id(reply: str, records: list[dict]) -> str:
    allowed = {row["id"] for row in records}
    found = MESH_ID.findall(reply or "")
    if len(found) == 1 and found[0] in allowed:
        return found[0]
    return ""


_FROZEN = {"kv1.3", "kcna3", "hgk5", "p22001"}
_KV_NAMES = ["Kv1.3", "KCNA3", "HGK5", "P22001"]


def frozen_target(target: str) -> bool:
    return target.strip().casefold() in _FROZEN


def target_names_for(target: str) -> list[str]:
    """Chain A is the first name. Kv1.3 keeps its four names; other targets use the typed name."""
    if frozen_target(target):
        return list(_KV_NAMES)
    name = target.strip()
    return [name] if name else []


def _scope_text(batch: Path, mesh: str, label: str, target: str, as_of: str | None = None) -> str:
    chains = (batch / "chains").resolve().as_posix()
    scope = CHAIN_SCOPE.resolve().as_posix()
    # Quote values so a label with a colon stays one field.
    # as_of comes from this run. Omitting it drops a parent date in load_scope.
    lines = [
        f"extends: {json.dumps(scope, ensure_ascii=False)}",
        f"case_root: {json.dumps(chains, ensure_ascii=False)}",
    ]
    if as_of:
        lines.append(f"as_of: {json.dumps(as_of, ensure_ascii=False)}")
    lines.extend(
        [
            "exclude_known: false",
            f"disease_mesh: {mesh}",
            f"disease_name: {json.dumps(label, ensure_ascii=False)}",
            f"target: {json.dumps(target, ensure_ascii=False)}",
            "target_names:",
        ]
    )
    lines.extend(f"  - {json.dumps(name, ensure_ascii=False)}" for name in target_names_for(target))
    lines.append("harvest_and: []")
    return "\n".join(lines) + "\n"


def _resolve_row(
    name: str,
    *,
    lookup,
    pick,
    used: set[str],
) -> tuple[str, str, str, dict]:
    """Return mesh, label, status, audit record for one count-table row.

    A transport error is tried once more. The second failure leaves the place
    empty so the next row can fill it.
    """
    record: dict = {"name": name, "records": [], "error": ""}
    records = None
    for _attempt in range(2):
        try:
            records, raw = lookup(name)
            record["raw"] = raw
            break
        except Exception as exc:  # noqa: BLE001 — one retry, then the next rank fills this place
            record["error"] = str(exc).splitlines()[0][:300]
            records = None
    if records is None:
        return "", "", "lookup_failed", record
    record["records"] = records
    if not records:
        return "", "", "no_match", record
    if len(records) == 1:
        chosen = records[0]["id"]
        label = records[0]["label"]
    else:
        try:
            reply = pick(name, records)
        except Exception as exc:  # noqa: BLE001
            reply = ""
            record["error"] = str(exc).splitlines()[0][:300]
        record["choice"] = reply
        chosen = _choice_id(reply, records)
        if not chosen:
            return "", "", "unmatched_choice", record
        label = next(row["label"] for row in records if row["id"] == chosen)
    if chosen in used:
        return chosen, label, "duplicate_mesh", record
    return chosen, label, "matched", record


def write_scopes(batch: Path, lookup=None, pick=_model_pick) -> dict:
    top5_path = batch / "nominate" / "top5.json"
    if not top5_path.is_file():
        raise SystemExit(f"mesh needs {top5_path}")
    payload = json.loads(top5_path.read_text(encoding="utf-8"))
    lookup = lookup or lookup_mesh_heading
    candidates = list(payload.get("candidates") or [])
    if not candidates:
        raise SystemExit(
            f"mesh failed: {top5_path} has no candidates. Run --stage top5 --counts <table> first."
        )
    meta = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
    target = str(meta.get("target") or "")
    as_of = str(meta.get("as_of") or "") or None
    want = int(payload.get("want") or WANT)
    mesh_dir = batch / "nominate" / "mesh"
    scope_dir = batch / "nominate" / "scopes"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    if scope_dir.is_dir():
        for old in scope_dir.glob("*.yaml"):
            old.unlink()
    used: set[str] = set()
    selected: list[dict] = []
    skipped: list[dict] = []
    for index, row in enumerate(candidates, 1):
        if len(selected) >= want:
            break
        name = str(row.get("name") or "")
        table_rank = int(row.get("rank") or index)
        stored = str(row.get("mesh") or "")
        if stored:
            label = name.split(" (", 1)[0].strip() or name
            status = "duplicate_mesh" if stored in used else "matched"
            mesh = stored
            record = {"name": name, "records": [{"id": stored, "label": name}], "error": ""}
        else:
            mesh, label, status, record = _resolve_row(name, lookup=lookup, pick=pick, used=used)
        (mesh_dir / f"{table_rank:02d}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if status != "matched":
            skipped.append(
                {
                    "rank": table_rank,
                    "name": name,
                    "part_a": row.get("part_a"),
                    "part_b": row.get("part_b"),
                    "part_c": row.get("part_c"),
                    "mesh": mesh,
                    "mesh_label": label,
                    "status": status,
                    "scope": "",
                }
            )
            continue
        used.add(mesh)
        scope_dir.mkdir(parents=True, exist_ok=True)
        scope_path = scope_dir / f"{mesh}.yaml"
        scope_path.write_text(
            _scope_text(batch, mesh, label, target, as_of=as_of), encoding="utf-8"
        )
        selected.append(
            {
                "rank": len(selected) + 1,
                "table_rank": table_rank,
                "name": name,
                "part_a": row.get("part_a"),
                "part_b": row.get("part_b"),
                "part_c": row.get("part_c"),
                "mesh": mesh,
                "mesh_label": label,
                "status": "matched",
                "scope": scope_path.relative_to(batch).as_posix(),
            }
        )
    payload["diseases"] = selected
    payload["skipped"] = skipped
    top5_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_top5_md(batch, selected)
    return payload


def _write_top5_md(batch: Path, selected: list[dict]) -> None:
    """The five diseases chain will run. Same rows as nominate/scopes."""
    lines = [
        "# Top 5",
        "",
        "Same MeSH id is one row. Runs is the number of nomination files, out of five.",
        "Chain reads these five MeSH ids.",
        "",
        "| # | Indication | Runs | MeSH |",
        "|---|---|---:|---|",
    ]
    for row in selected:
        lines.append(
            f"| {row['rank']} | {row['mesh_label']} | {row.get('part_a') if row.get('part_a') is not None else ''} | {row['mesh']} |"
        )
    (batch / "nominate" / "top5.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def matched_scopes(batch: Path) -> list[Path]:
    payload = json.loads((batch / "nominate" / "top5.json").read_text(encoding="utf-8"))
    paths = []
    chains = (batch / "chains").resolve()
    for disease in payload.get("diseases") or []:
        if disease.get("status") != "matched" or not disease.get("scope"):
            continue
        path = batch / str(disease["scope"])
        text = path.read_text(encoding="utf-8")
        if f'"{chains.as_posix()}"' not in text and chains.as_posix() not in text:
            raise SystemExit(f"{path} does not write under {chains}")
        paths.append(path)
    return paths


def _self_check() -> None:
    import tempfile

    hepatitis = _ranked(
        "Autoimmune hepatitis",
        [
            {"id": "D019693", "label": "Hepatitis, Autoimmune"},
            {"id": "D001327", "label": "Autoimmune Diseases"},
        ],
    )
    assert [row["id"] for row in hepatitis] == ["D019693"]
    sjogren = _ranked(
        "Sjogren syndrome",
        [
            {"id": "D012859", "label": "Sjogren's Syndrome"},
            {"id": "D016111", "label": "Sjogren-Larsson Syndrome"},
        ],
    )
    assert [row["id"] for row in sjogren] == ["D012859"]
    assert _ranked("Non-infectious uveitis", [{"id": "D000073296", "label": "Noncommunicable Diseases"}]) == []

    sample = """# DeepSeek-V4-Pro, five runs

| # | Indication (merged name) | Files appearing in Part A | Files appearing in Part B | Files naming it in Part C |
|---|---|---|---|---|
| 1 | IgA nephropathy (Berger disease) | 5 | 5 | 5 |
| 2 | ANCA-associated vasculitis (GPA/MPA) | 5 | 5 | 5 |
| 3 | Pemphigus vulgaris/foliaceus | 5 | 5 | 5 |
| 4 | Giant cell arteritis | 4 | 4 | 4 |
| 5 | Sarcoidosis | 4 | 4 | 4 |
| 6 | Celiac disease (including refractory celiac disease) | 4 | 4 | 4 |
"""
    parsed = parse_counts_md(sample)
    assert len(parsed) == 6
    assert parsed[0]["name"] == "IgA nephropathy (Berger disease)"

    try:
        write_top5(Path("."), counts_path=None)
        raise AssertionError("missing counts must fail")
    except SystemExit as exc:
        assert "no merged count table" in str(exc)

    with tempfile.TemporaryDirectory() as tmp:
        batch = Path(tmp)
        (batch / "batch.json").write_text(
            json.dumps({"target": "KCNA3", "as_of": "2026-04-30"}) + "\n", encoding="utf-8"
        )
        src = Path(tmp) / "counts-src.md"
        src.write_text(sample, encoding="utf-8")
        payload = write_top5(batch, counts_path=src)
        assert payload["candidates"][0]["name"] == "IgA nephropathy (Berger disease)"
        assert len(payload["candidates"]) == 6
        assert payload["diseases"] == []
        assert not (batch / "nominate" / "counts.md").exists()
        assert payload["source"] == str(src.resolve())

        short = Path(tmp) / "short.md"
        short.write_text(
            "| # | Indication (merged name) | Files appearing in Part A | Files appearing in Part B | Files naming it in Part C |\n"
            "|---|---|---|---|---|\n"
            "| 1 | Sarcoidosis | 2 | 2 | 2 |\n"
            "| 2 | Giant cell arteritis | 1 | 1 | 1 |\n",
            encoding="utf-8",
        )
        few = write_top5(batch, counts_path=short)
        assert len(few["candidates"]) == 2

        def fake_lookup(name: str):
            if "IgA" in name:
                return [{"id": "D005922", "label": "Glomerulonephritis, IGA"}], {}
            if "ANCA" in name:
                return [{"id": "D056648", "label": "Anti-Neutrophil Cytoplasmic Antibody-Associated Vasculitis"}], {}
            if "Pemphigus" in name:
                return [{"id": "D010392", "label": "Pemphigus"}], {}
            if "Giant" in name:
                return [
                    {"id": "D013700", "label": "Giant Cell Arteritis"},
                    {"id": "D001234", "label": "Other"},
                ], {}
            if name == "Sarcoidosis":
                return [{"id": "D013700", "label": "Giant Cell Arteritis"}], {}
            if "Celiac" in name:
                return [{"id": "D002446", "label": "Celiac Disease"}], {}
            return [], {}

        def fake_pick(name: str, records: list[dict]) -> str:
            assert "Giant" in name
            return "D013700"

        write_top5(batch, counts_path=src)
        scoped = write_scopes(batch, lookup=fake_lookup, pick=fake_pick)
        names = [row["name"] for row in scoped["diseases"]]
        assert names == [
            "IgA nephropathy (Berger disease)",
            "ANCA-associated vasculitis (GPA/MPA)",
            "Pemphigus vulgaris/foliaceus",
            "Giant cell arteritis",
            "Celiac disease (including refractory celiac disease)",
        ], names
        assert scoped["diseases"][4]["mesh"] == "D002446"
        assert any(row["name"] == "Sarcoidosis" and row["status"] == "duplicate_mesh" for row in scoped["skipped"])
        assert len(matched_scopes(batch)) == 5
        kv_scope = (batch / "nominate" / "scopes" / "D005922.yaml").read_text(encoding="utf-8")
        assert 'as_of: "2026-04-30"' in kv_scope
        assert "Kv1.3" in kv_scope

        missed = write_scopes(
            batch,
            lookup=lambda name: ([{"id": "D012507", "label": "Sarcoidosis"}, {"id": "D001234", "label": "Other"}], {}),
            pick=lambda name, records: "D999999",
        )
        assert missed["diseases"] == []
        assert len(missed["skipped"]) == 6
        assert all(row["status"] == "unmatched_choice" for row in missed["skipped"])
        assert not list((batch / "nominate" / "scopes").glob("*.yaml"))

        one = parse_lookup([{"resource": "http://id.nlm.nih.gov/mesh/D012507", "label": "Sarcoidosis"}])
        assert one == [{"id": "D012507", "label": "Sarcoidosis"}]
        assert parse_lookup([]) == []
        assert "IgA nephropathy" in query_variants("IgA nephropathy (Berger disease)")
        assert "Pemphigus" in query_variants("Pemphigus vulgaris/foliaceus")
        text = _scope_text(batch, "D005922", "Glomerulonephritis, IGA", "KCNA3", as_of="2019-01-01")
        assert 'as_of: "2019-01-01"' in text
        assert "target_names:" in text
        other = _scope_text(batch, "D006937", "Hypercholesterolemia", "PCSK9", as_of="2018-06-01")
        assert 'as_of: "2018-06-01"' in other
        assert "PCSK9" in other
        assert "Kv1.3" not in other
        plain = _scope_text(batch, "D006937", "Hypercholesterolemia", "PCSK9")
        assert "as_of:" not in plain
        assert "调整推理过程" in other
        assert "\\u5efa" not in other
        tries = {"n": 0}

        def flaky_lookup(name: str):
            tries["n"] += 1
            if tries["n"] == 1:
                raise TimeoutError("down")
            return [{"id": "D002446", "label": "Celiac Disease"}], {}

        mesh, _label, status, _record = _resolve_row(
            "Celiac disease", lookup=flaky_lookup, pick=fake_pick, used=set()
        )
        assert status == "matched" and mesh == "D002446" and tries["n"] == 2
        _mesh, _label, status, _record = _resolve_row(
            "Celiac disease",
            lookup=lambda _name: (_ for _ in ()).throw(TimeoutError("down")),
            pick=fake_pick,
            used=set(),
        )
        assert status == "lookup_failed"
    print("self_check_ok")


if __name__ == "__main__":
    import sys

    if "--self-check" in sys.argv:
        _self_check()
