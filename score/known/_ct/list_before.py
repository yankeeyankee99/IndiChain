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


groups = {
    "IL17A <2016-07-11": (["secukinumab", "ixekizumab", "bimekizumab"], "2016-07-11"),
    "IL4R <2015-05-12": (["dupilumab"], "2015-05-12"),
    "IL4R <2020-10-28": (["dupilumab"], "2020-10-28"),
    "CFB <2018-02-07": (["iptacopan", "LNP023", "IONIS_FB_LRx"], "2018-02-07"),
    "C5 <2008-10-01": (["eculizumab", "pexelizumab"], "2008-10-01"),
    "TSLP <2021-04-22": (["tezepelumab"], "2021-04-22"),
}

for label, (files, freeze) in groups.items():
    print("\n====", label)
    seen = {}
    for f in files:
        for row in load(f):
            seen[row["nct"]] = row
    rows = [
        r
        for r in seen.values()
        if norm(r["start"]) < freeze and not healthy(r)
    ]
    rows.sort(key=lambda r: (norm(r["start"]), r["nct"]))
    print("trials", len(rows))
    for r in rows:
        cond = " | ".join(r["conds"][:3])
        print(f"{r['start']:10} {r['nct']} {cond[:70]}")
        print(f"           {r['title'][:120]}")
