import json
from pathlib import Path

root = Path(__file__).resolve().parent


def load(name):
    data = json.loads((root / f"studies_{name}.json").read_text(encoding="utf-8"))
    rows = []
    for st in data["studies"]:
        p = st["protocolSection"]
        ident = p["identificationModule"]
        status = p.get("statusModule") or {}
        conds = (p.get("conditionsModule") or {}).get("conditions") or []
        rows.append(
            {
                "nct": ident.get("nctId"),
                "title": ident.get("briefTitle") or "",
                "post": (status.get("studyFirstPostDateStruct") or {}).get("date") or "",
                "start": (status.get("startDateStruct") or {}).get("date") or "",
                "conds": conds,
                "status": status.get("overallStatus") or "",
            }
        )
    return rows


def norm(d):
    if not d:
        return "9999-99-99"
    if len(d) == 7:
        return d + "-01"
    if len(d) == 4:
        return d + "-01-01"
    return d


def healthy(row):
    disease = [
        c
        for c in row["conds"]
        if "healthy" not in c.lower() and "volunteer" not in c.lower()
    ]
    return not disease


needles = (
    "hidradenitis",
    "eosinophilic esophagitis",
    "bullous",
    "iga nephropathy",
    "myasthenia",
    "nasal polyp",
    "churg",
    "granulomat",
)
print("--- validation and nearby ---")
for f in [
    "secukinumab",
    "ixekizumab",
    "dupilumab",
    "iptacopan",
    "LNP023",
    "IONIS_FB_LRx",
    "eculizumab",
    "tezepelumab",
    "benralizumab",
]:
    for row in load(f):
        blob = (row["title"] + " " + " ".join(row["conds"])).lower()
        if any(n in blob for n in needles):
            print(
                f"{f} {row['nct']} start {row['start']} post {row['post']} {row['status']}"
            )
            print("   ", row["title"][:140])
            print("   ", "; ".join(row["conds"][:6]))
