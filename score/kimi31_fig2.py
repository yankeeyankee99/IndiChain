"""Kimi K3.1 Pro on the Fig. 2 pairs, then the three ordinary-model scores.

Nomination for all ten pairs. Mechanism text for the seven whose first
complete nomination contained the later indication. Coding Plan model
kimi-k3-1-pro-261024, temperature 1, thinking enabled. Does not write the key.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import ssl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ordinary_bench import CHAIN_PROMPT, NOMINATE  # noqa: E402
from bench_percent import (  # noqa: E402
    PAIRS as BENCH_PAIRS,
    Papers,
    hits_target,
    identifiers,
    judge_sentence,
    novelty,
    on_or_before,
    parse_chain,
    parse_nominate,
    percent,
    write_html,
    write_report_docx,
)

ROOT = Path(r"c:\Users\YK\Desktop\INDICATOR\投稿文件整理\干实验数据\验证对照\11.17Kimi K3.1")
BASELINE = Path(r"c:\Users\YK\Desktop\INDICATOR\10.29调整推理过程\打分api普通模型\11.8deepseek\percent.json")
ENV = Path(r"c:\Users\YK\Desktop\INDICATOR\10.29调整推理过程\nominate\.env.txt")
MODEL = "kimi-k3-1-pro-261024"
CODING = "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"
WORKERS = 4
TEMPERATURE = 1

# chain=True: the seven pairs scored in Fig. 2b–d.
PAIRS = (
    ("il17a-hidradenitis", "IL17A", "Hidradenitis Suppurativa", "2016-07-11", "known/IL17A.json", True),
    ("il4r-eoe", "IL4R", "Eosinophilic Esophagitis", "2015-05-12", "known/IL4R-2015-05-12.json", True),
    ("il4r-bullous", "IL4R", "Pemphigoid, Bullous", "2020-10-28", "known/IL4R-2020-10-28.json", True),
    ("cfb-igan", "CFB", "Glomerulonephritis, IGA", "2018-02-07", "known/CFB.json", True),
    ("c5-mg", "C5", "Myasthenia Gravis", "2008-10-01", "known/C5-2008-10-01.json", True),
    ("c5-nmo", "C5", "Neuromyelitis Optica", "2009-04-01", "known/C5-2009-04-01.json", True),
    ("tslp-nasal-polyps", "TSLP", "Nasal Polyps", "2021-04-22", "known/TSLP.json", True),
    ("cd19-igg4", "CD19", "Immunoglobulin G4-Related Disease", "2016-03-01", "", False),
    ("il5-copd", "IL5", "Pulmonary Disease, Chronic Obstructive", "2012-01-01", "", False),
    ("il5ra-churg-strauss", "IL5RA", "Churg-Strauss Syndrome", "2017-04-15", "known/IL5RA.json", False),
)

EXTRA_HIT = {
    "cd19-igg4": ("immunoglobulin g4-related disease", "igg4-related disease", "igg4 rd"),
    "il5-copd": ("pulmonary disease chronic obstructive", "chronic obstructive pulmonary", "copd"),
    "il5ra-churg-strauss": (
        "churg-strauss",
        "eosinophilic granulomatosis with polyangiitis",
        "egpa",
    ),
}


def _check() -> None:
    assert MODEL == "kimi-k3-1-pro-261024"
    assert TEMPERATURE == 1
    assert len(PAIRS) == 10
    assert sum(1 for row in PAIRS if row[5]) == 7
    assert "List 5 specific diseases" in NOMINATE
    assert "original sentence from that paper's abstract" in CHAIN_PROMPT
    scored = {row[0] for row in BENCH_PAIRS}
    for slug, _t, _d, _a, known, chain in PAIRS:
        if chain:
            assert slug in scored and slug != "kv1.3-igan"
            assert (HERE / known).is_file()
    assert _hit("cd19-igg4", ["IgG4-related disease"])
    assert not _hit("il5-copd", ["Severe eosinophilic asthma"])
    print("check ok", flush=True)


def _load_key() -> str:
    for line in ENV.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("ARK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("ARK_API_KEY missing")


def _safe(exc: BaseException, key: str) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if key and key in text:
        text = text.replace(key, "[redacted]")
    return text[:500]


def _http_json(url: str, data: bytes, headers: dict, timeout: int) -> dict:
    last: BaseException | None = None
    for attempt in range(6):
        try:
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
            last = exc
        except (
            URLError,
            TimeoutError,
            ConnectionResetError,
            RemoteDisconnected,
            IncompleteRead,
            ssl.SSLError,
        ) as exc:
            last = exc
        time.sleep(min(60, 2 ** attempt))
    assert last is not None
    raise last


def _call(prompt: str, key: str, timeout: int) -> tuple[str, str]:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "thinking": {"type": "enabled"},
    }
    body = _http_json(
        CODING,
        json.dumps(payload).encode(),
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout,
    )
    choice = body["choices"][0]
    message = choice["message"]
    content = str(message.get("content") or "").strip()
    if not content:
        reasoning = str(message.get("reasoning_content") or "")
        raise RuntimeError(
            f"empty content finish={choice.get('finish_reason')} "
            f"reasoning_chars={len(reasoning)} head={reasoning[:160]!r}"
        )
    returned = str(body.get("model") or "")
    return content, returned


def _job(kind: str, slug: str, target: str, disease: str, as_of: str, key: str) -> tuple[str, str, str, str]:
    if kind == "nominate":
        prompt = NOMINATE.format(target=target, as_of=as_of)
        timeout = 1200
    else:
        prompt = CHAIN_PROMPT.format(target=target, disease=disease, as_of=as_of)
        timeout = 2400
    print(f"start {kind} {slug}", flush=True)
    try:
        text, returned = _call(prompt, key, timeout)
    except Exception as exc:
        raise RuntimeError(f"{slug} {kind}: {exc}") from None
    print(f"done {kind} {slug} chars {len(text)} model {returned}", flush=True)
    return slug, kind, text, returned


def _write_calls(returned: str) -> None:
    for slug, target, disease, as_of, _known, chain in PAIRS:
        out = ROOT / slug
        if not (out / "nominate.txt").is_file():
            continue
        if chain and not (out / "chain.txt").is_file():
            continue
        record = {
            "model": MODEL,
            "returned_model": returned,
            "provider": "volcengine-coding-plan",
            "temperature": TEMPERATURE,
            "thinking": "enabled",
            "target": target,
            "disease": disease,
            "as_of": as_of,
            "chain": chain,
            "called_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nominate_file": "nominate.txt",
            "chain_file": "chain.txt" if chain else "",
        }
        (out / "calls.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _hit(slug: str, names: list[str]) -> bool:
    if slug in EXTRA_HIT:
        from bench_percent import norm
        aliases = tuple(norm(alias) for alias in EXTRA_HIT[slug])
        for name in names:
            folded = f" {norm(name)} "
            if any(f" {alias} " in folded for alias in aliases):
                return True
        return False
    return hits_target(slug, names)


def score(label: str = "kimi-k3.1") -> None:
    baseline = {
        row["slug"]: row
        for row in json.loads(BASELINE.read_text(encoding="utf-8"))
    }
    papers = Papers()
    summary = []
    nominate_rows = []
    for slug, _target, _disease, as_of, known_rel, chain in PAIRS:
        text = (ROOT / slug / "nominate.txt").read_text(encoding="utf-8")
        names = parse_nominate(text)
        if len(names) != 5:
            raise SystemExit(f"{slug} nominate parsed {len(names)}")
        hit = _hit(slug, [row["name"] for row in names])
        novel = None
        if known_rel:
            known = json.loads((HERE / known_rel).read_text(encoding="utf-8"))
            novel = novelty(names, known)
        nominate_rows.append({
            "slug": slug,
            "as_of": as_of,
            "chain": chain,
            "hit": hit,
            "names": [row["name"] for row in names],
            "novel": None if novel is None else percent(novel["new"], novel["of"]),
            "novel_new": None if novel is None else novel["new"],
            "flags": None if novel is None else novel["flags"],
        })
        print(f"nominate {slug} hit {hit} names {nominate_rows[-1]['names']}", flush=True)
        if not chain:
            continue
        ord_sent = parse_chain((ROOT / slug / "chain.txt").read_text(encoding="utf-8"))
        unique = {}
        for row in ord_sent:
            doi, pmid = identifiers(row["source"])
            row["doi"], row["pmid"] = doi, pmid
            key = (doi, pmid, "" if doi or pmid else row.get("source") or "")
            row["_key"] = key
            unique[key] = (doi, pmid, key[2])
        resolved = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(papers.resolve, doi, pmid, source): (doi, pmid, source)
                for doi, pmid, source in unique.values()
                if doi or pmid or source
            }
            done = 0
            for fut in as_completed(futures):
                resolved[futures[fut]] = fut.result()
                done += 1
                if done % 25 == 0 or done == len(futures):
                    print(f"  {slug} papers {done}/{len(futures)}", flush=True)
        counts = {"in": 0, "den": 0, "unresolved": 0, "absent": 0, "freeze_ok": 0, "freeze_den": 0}
        for row in ord_sent:
            paper = resolved.get(row["_key"], {"resolved": False, "date": ""})
            mark = judge_sentence(row, paper)
            counts["den"] += 1
            counts["in"] += mark == "in"
            counts["unresolved"] += mark == "unresolved"
            counts["absent"] += mark == "absent"
            counts["freeze_den"] += 1
            verdict = on_or_before(paper.get("date") or "", as_of) if paper.get("resolved") else None
            counts["freeze_ok"] += verdict is True
        indi = baseline[slug]["indichain"]
        summary.append({
            "slug": slug,
            "as_of": as_of,
            "indichain": indi,
            "ordinary": {
                "novel": nominate_rows[-1]["novel"],
                "novel_new": nominate_rows[-1]["novel_new"],
                "names": nominate_rows[-1]["names"],
                "flags": nominate_rows[-1]["flags"],
                "sentence": percent(counts["in"], counts["den"]),
                "sentence_in": counts["in"],
                "sentence_den": counts["den"],
                "unresolved": counts["unresolved"],
                "absent": counts["absent"],
                "freeze": percent(counts["freeze_ok"], counts["freeze_den"]),
                "freeze_ok": counts["freeze_ok"],
                "freeze_den": counts["freeze_den"],
                "sentences": len(ord_sent),
            },
        })
        print(
            f"score {slug} new {indi['novel_new']}/5 vs {nominate_rows[-1]['novel_new']}/5 "
            f"sentence {counts['in']}/{counts['den']} freeze {counts['freeze_ok']}/{counts['freeze_den']}",
            flush=True,
        )
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "nominate.json").write_text(
        json.dumps(nominate_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (ROOT / "percent.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_html(ROOT / "percent.html", summary, label)
    write_report_docx(ROOT / "three-metric-percentages.docx", summary, label)
    print(f"wrote {ROOT}", flush=True)


def main() -> None:
    key = _load_key()
    ROOT.mkdir(parents=True, exist_ok=True)
    pending = []
    for slug, target, disease, as_of, _known, chain in PAIRS:
        out = ROOT / slug
        out.mkdir(parents=True, exist_ok=True)
        if not (out / "nominate.txt").is_file():
            pending.append(("nominate", slug, target, disease, as_of))
        if chain and not (out / "chain.txt").is_file():
            pending.append(("chain", slug, target, disease, as_of))
    print(f"pending {len(pending)}", flush=True)
    failed = []
    returned = ""
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(_job, kind, slug, target, disease, as_of, key)
            for kind, slug, target, disease, as_of in pending
        ]
        for fut in as_completed(futures):
            try:
                slug, kind, text, returned = fut.result()
                (ROOT / slug / f"{kind}.txt").write_text(text + "\n", encoding="utf-8")
            except Exception as exc:
                failed.append(_safe(exc, key))
                print(f"fail {_safe(exc, key)}", flush=True)
    _write_calls(returned)
    if failed:
        raise SystemExit("failed " + " | ".join(failed))
    score()


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    elif "--score" in sys.argv:
        score()
    else:
        main()
