"""Hand the user's sentence to the model, run the library calls it asks for, and keep the card it writes."""
from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import urlopen

# ponytail: eight replies then stop. A longer investigation needs a higher ceiling, not a scripted search.
ROUNDS = 8

TASK = """用户说：
{utterance}

后续 chain 需要这个靶点在库里的编号，用来避免把靶点本身提出为节点。
你可以调用 MeSH 和 UniProt。一次回复只写下面三种之一。

MESH 检索式
UNIPROT 检索式
BACKGROUND
{{"target":"","mesh":"","uniprot":"","names":[]}}

检索式整段由你写。程序原样发给对应的库，并把库的返回作为下一条消息给你。
names 里放你要交给 chain 的写法。
"""


def _get(url: str) -> dict:
    with urlopen(url, timeout=60) as response:
        return json.load(response)


def live_mesh(term: str) -> dict:
    payload = _get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        + urlencode({"db": "mesh", "term": term, "retmode": "json", "retmax": 5})
    )
    result = payload.get("esearchresult") or {}
    ids = [str(item) for item in (result.get("idlist") or [])]
    records = []
    if ids:
        summary = _get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
            + urlencode({"db": "mesh", "id": ",".join(ids), "retmode": "json"})
        )
        rows = summary.get("result") or {}
        for uid in ids:
            row = rows.get(uid) or {}
            records.append({"uid": uid, "mesh": row.get("ds_meshui") or "", "terms": row.get("ds_meshterms") or []})
    return {"term": term, "count": str(result.get("count") or ""), "records": records}


def live_uniprot(query: str) -> dict:
    payload = _get(
        "https://rest.uniprot.org/uniprotkb/search?"
        + urlencode({"query": query, "format": "json", "size": 5})
    )
    rows = []
    for row in payload.get("results") or []:
        genes = row.get("genes") or []
        gene = ""
        if genes and (genes[0].get("geneName") or {}).get("value"):
            gene = genes[0]["geneName"]["value"]
        recommended = (
            ((row.get("proteinDescription") or {}).get("recommendedName") or {}).get("fullName") or {}
        ).get("value") or ""
        rows.append({"accession": row.get("primaryAccession") or "", "gene": gene, "recommended": recommended})
    return {"query": query, "returned": len(rows), "rows": rows}


def _command(reply: str) -> tuple[str, str]:
    lines = [line.strip() for line in (reply or "").splitlines() if line.strip()]
    if not lines:
        return "", ""
    head, _, rest = lines[0].partition(" ")
    key = head.upper()
    if key in {"MESH", "UNIPROT"}:
        follow = " ".join(lines[1:]).strip()
        if rest.strip() in {"", "检索式"}:
            return key, follow
        return key, rest.strip()
    if key == "BACKGROUND":
        return "BACKGROUND", rest or "\n".join(lines[1:])
    return "", ""


def _card(body: str) -> dict:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("BACKGROUND is not an object")
    names = data.get("names") or []
    if isinstance(names, str):
        names = [names]
    return {
        "target": " ".join(str(data.get("target") or "").split()),
        "mesh": " ".join(str(data.get("mesh") or "").split()),
        "uniprot": " ".join(str(data.get("uniprot") or "").split()),
        "names": [" ".join(str(item).split()) for item in names if str(item).strip()],
    }


def background_names(names: list[str], background: dict) -> list[str]:
    out = list(names)
    seen = {item.casefold() for item in out}
    for item in [background.get("target") or "", *(background.get("names") or [])]:
        text = " ".join(str(item).split())
        if text and text.casefold() not in seen:
            out.append(text)
            seen.add(text.casefold())
    return out


def background_record(background: dict) -> dict | None:
    record = {
        "mesh": " ".join(str(background.get("mesh") or "").split()),
        "uniprot": " ".join(str(background.get("uniprot") or "").split()),
    }
    if record["mesh"] or record["uniprot"]:
        return record
    return None


def chain_identity(names: list[str], looked_up: dict, background: dict | None) -> tuple[list[str], dict]:
    """The model's card supplies the chain's target id. Without a card, the chain keeps its own lookup."""
    if not background:
        return list(names), looked_up
    return background_names(names, background), background_record(background) or looked_up


def ask_background(utterance: str, ask, mesh_search=live_mesh, uniprot_search=live_uniprot) -> dict:
    messages = [{"role": "user", "content": TASK.format(utterance=utterance)}]
    for _ in range(ROUNDS):
        reply = ask(messages)
        messages.append({"role": "assistant", "content": reply})
        kind, body = _command(reply)
        if kind == "MESH":
            result = mesh_search(body)
        elif kind == "UNIPROT":
            result = uniprot_search(body)
        elif kind == "BACKGROUND":
            try:
                return _card(body)
            except (ValueError, json.JSONDecodeError):
                result = {"error": "BACKGROUND was not a JSON object"}
        else:
            result = {"error": "reply was not MESH, UNIPROT, or BACKGROUND"}
        messages.append({"role": "user", "content": json.dumps(result, ensure_ascii=False)})
    raise RuntimeError("model did not write BACKGROUND")


def _self_check() -> None:
    assert "Kv1.3" not in TASK and "D051664" not in TASK and "首先" not in TASK
    assert _command("UNIPROT 检索式\ngene:EGFR AND organism_id:9606") == (
        "UNIPROT",
        "gene:EGFR AND organism_id:9606",
    )
    assert _command("MESH Kv1.3[All Fields]") == ("MESH", "Kv1.3[All Fields]")
    seen = []

    def mesh_search(term: str) -> dict:
        seen.append(("MESH", term))
        return {"term": term, "count": "0", "records": []}

    def uniprot_search(query: str) -> dict:
        seen.append(("UNIPROT", query))
        return {"query": query, "returned": 1, "rows": [{"accession": "P22001", "gene": "KCNA3", "recommended": "channel"}]}

    def ask(messages: list[dict]) -> str:
        if len(messages) == 1:
            assert "探索 Kv1.3 的新适应症" in messages[0]["content"]
            return "MESH Kv1.3[All Fields]"
        if len(messages) == 3:
            assert '"count": "0"' in messages[-1]["content"]
            return "UNIPROT protein_name:Kv1.3"
        assert "P22001" in messages[-1]["content"]
        return 'BACKGROUND\n{"target":"Kv1.3","mesh":"D051664","uniprot":"P22001","names":["KCNA3"]}'

    card = ask_background("探索 Kv1.3 的新适应症", ask, mesh_search, uniprot_search)
    assert seen == [("MESH", "Kv1.3[All Fields]"), ("UNIPROT", "protein_name:Kv1.3")]
    assert card == {"target": "Kv1.3", "mesh": "D051664", "uniprot": "P22001", "names": ["KCNA3"]}
    names, record = chain_identity(["other"], {"mesh": "", "uniprot": ""}, card)
    assert record == {"mesh": "D051664", "uniprot": "P22001"}
    assert "KCNA3" in names
    print("self-check ok")


if __name__ == "__main__":
    _self_check()
