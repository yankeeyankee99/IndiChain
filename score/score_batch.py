"""Score one batch. One number per disease, from every hop its finished runs wrote."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from score_hops import ITEMS, consensus_chain, papers_by_id, score_runs

SLOTS = tuple(f"result-{n}" for n in range(1, 6))


def _folders(chains: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not chains.is_dir():
        return found
    for path in chains.rglob("chain.json"):
        slot = path.parent
        if slot.name.startswith("result-"):
            found.setdefault(slot.parent.name, slot.parent)
    for path in chains.rglob("target_library.json"):
        found.setdefault(path.parent.name, path.parent)
    return found


def _rows(batch: Path) -> list[tuple[str, str, Path | None]]:
    found = _folders(batch / "chains")
    rows: list[tuple[str, str, Path | None]] = []
    seen: set[str] = set()
    top5 = batch / "nominate" / "top5.json"
    if top5.is_file():
        payload = json.loads(top5.read_text(encoding="utf-8"))
        for disease in payload.get("diseases") or []:
            if disease.get("status") != "matched":
                continue
            mesh = str(disease.get("mesh") or "")
            if not mesh or mesh in seen:
                continue
            seen.add(mesh)
            rows.append((mesh, str(disease.get("name") or ""), found.get(mesh)))
    for mesh in sorted(found):
        if mesh not in seen:
            rows.append((mesh, "", found[mesh]))
    return rows


def score_batch(batch: Path) -> dict:
    batch = batch.resolve()
    if not (batch / "batch.json").is_file() and not (batch / "nominate" / "top5.json").is_file():
        raise SystemExit(f"not a batch: {batch}")
    diseases = []
    for mesh, name, folder in _rows(batch):
        present = []
        if folder is not None:
            present = [slot for slot in SLOTS if (folder / slot / "chain.json").is_file()]
        empty = [slot for slot in SLOTS if slot not in present]
        entry = {
            "mesh": mesh,
            "name": name,
            "score": None,
            "items": {item: 0.0 for item in ITEMS},
            "written_hops": 0,
            "empty_slots": empty,
            "consensus": [],
        }
        if folder is not None and present:
            runs = []
            for slot in present:
                chain = json.loads((folder / slot / "chain.json").read_text(encoding="utf-8"))
                runs.append(chain)
            papers = papers_by_id(folder, folder)
            scored = score_runs(runs, papers)
            entry["score"] = scored["score"]
            entry["items"] = scored["items"]
            entry["written_hops"] = len(scored["hops"])
            entry["consensus"] = [step.get("step") for step in consensus_chain(runs).get("steps") or []]
        diseases.append(entry)
    report = {"items": list(ITEMS), "weight": 1, "max": len(ITEMS), "diseases": diseases}
    out = batch / "score"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "scores.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in diseases:
        score = "none" if row["score"] is None else f"{row['score']:.3f}"
        print(f"{row['mesh']} {score} hops {row['written_hops']} empty {','.join(row['empty_slots']) or '-'}")
    print(dest)
    return report


def _self_check() -> None:
    import tempfile

    quote = (
        "Proprotein convertase subtilisin/kexin type 9 (PCSK9) is a crucial protein "
        "governing the circulating levels of low density lipoprotein-cholesterol (LDL-C), "
        "by virtue of its pivotal role in the degradation of the LDL receptor (LDLR)."
    )
    with tempfile.TemporaryDirectory() as tmp:
        batch = Path(tmp)
        (batch / "batch.json").write_text("{}\n", encoding="utf-8")
        nominate = batch / "nominate"
        nominate.mkdir()
        (nominate / "top5.json").write_text(
            json.dumps(
                {
                    "diseases": [
                        {"mesh": "D006937", "name": "Hypercholesterolemia", "status": "matched"},
                        {"mesh": "D000000", "name": "Missing", "status": "matched"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        folder = batch / "chains" / "pcsk9" / "D006937"
        slot = folder / "result-1"
        slot.mkdir(parents=True)
        (folder / "target_library.json").write_text(
            json.dumps({"lines": [{"paper_id": "30522786", "line": quote}]}) + "\n",
            encoding="utf-8",
        )
        (slot / "chain.json").write_text(
            json.dumps(
                {
                    "target": "PCSK9",
                    "name": "Hypercholesterolemia",
                    "reached": True,
                    "steps": [
                        {
                            "step": "PCSK9 → LDL receptor (LDLR)",
                            "quotes": [quote],
                            "paper_id": "30522786",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        report = score_batch(batch)
        assert [row["mesh"] for row in report["diseases"]] == ["D006937", "D000000"]
        assert report["diseases"][0]["score"] == 7.0
        assert report["diseases"][0]["empty_slots"] == ["result-2", "result-3", "result-4", "result-5"]
        assert report["diseases"][0]["consensus"] == ["PCSK9 → LDL receptor (LDLR)"]
        assert report["diseases"][1]["score"] is None
        assert len(report["diseases"][1]["empty_slots"]) == 5
    print("self_check_ok")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    elif "--batch" in sys.argv:
        score_batch(Path(sys.argv[sys.argv.index("--batch") + 1]))
    else:
        raise SystemExit("pass --batch <campaign>")
