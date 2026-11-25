"""Ordinary-model benchmark on the pairs machine.

Coding Plan only: deepseek-v4-pro, temperature 0, thinking enabled.
Ignores Accesskey so the Bohrium gateway is not used. Does not write the key.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path("/root/ordinary-118/out")
ENV = Path("/root/ordinary-rest/env.txt")
MODEL = "deepseek-v4-pro"
CODING = "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"
WORKERS = 8

PAIRS = (
    ("kv1.3-igan", "Kv1.3", "IgA nephropathy", "2026-04-30"),
    ("il17a-hidradenitis", "IL17A", "Hidradenitis Suppurativa", "2016-07-11"),
    ("il4r-eoe", "IL4R", "Eosinophilic Esophagitis", "2015-05-12"),
    ("il4r-bullous", "IL4R", "Pemphigoid, Bullous", "2020-10-28"),
    ("cfb-igan", "CFB", "Glomerulonephritis, IGA", "2018-02-07"),
    ("c5-mg", "C5", "Myasthenia Gravis", "2008-10-01"),
    ("c5-nmo", "C5", "Neuromyelitis Optica", "2009-04-01"),
    ("tslp-nasal-polyps", "TSLP", "Nasal Polyps", "2021-04-22"),
)

NOMINATE = """Target: {target}. Use only literature available on or before {as_of}. Do not choose a disease because of an approval, trial, review, or finding that comes after this date.

Find new therapeutic indications for this target. List 5 specific diseases, ordered from the one you would test first to the one you would test fifth. Use English disease names. Put each disease on its own line:

1. Disease name
2. Disease name
3. Disease name
4. Disease name
5. Disease name
"""

CHAIN_PROMPT = """Target: {target}. Disease: {disease}. Use only literature available on or before {as_of}. Do not use an approval, trial, review, or finding that comes after this date as a reason for a step.

Write every mechanistic path you consider possible from this target to this disease. Start each path at the target and end it at the disease. Each step moves from one entity to the next. Do not write a step that has no literature. For each step, give more than one source. For each source, copy one original sentence from that paper's abstract that shows the left leads to the right. Use English. Put each source on its own line, and separate paths with a blank line:

Path 1
Left → Right
Original abstract sentence | Source
Original abstract sentence | Source
"""


def _load_key() -> str:
    for line in ENV.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("ARK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("ARK_API_KEY missing")


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


def _call(prompt: str, key: str, timeout: int) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "thinking": {"type": "enabled"},
    }
    body = _http_json(
        CODING,
        json.dumps(payload).encode(),
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout,
    )
    return str(body["choices"][0]["message"].get("content") or "")


def _safe(exc: BaseException, key: str) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if key and key in text:
        text = text.replace(key, "[redacted]")
    return text[:400]


def _job(kind: str, slug: str, target: str, disease: str, as_of: str, key: str) -> tuple[str, str, str]:
    if kind == "nominate":
        prompt = NOMINATE.format(target=target, as_of=as_of)
        timeout = 1200
    else:
        prompt = CHAIN_PROMPT.format(target=target, disease=disease, as_of=as_of)
        timeout = 2400
    print(f"start {kind} {slug}", flush=True)
    try:
        text = _call(prompt, key, timeout)
    except Exception as exc:
        raise RuntimeError(f"{slug} {kind}: {_safe(exc, key)}") from None
    print(f"done {kind} {slug} chars {len(text)}", flush=True)
    return slug, kind, text


def main() -> None:
    chain_only = "--chain-only" in sys.argv
    key = _load_key()
    ROOT.mkdir(parents=True, exist_ok=True)
    pending = []
    for slug, target, disease, as_of in PAIRS:
        out = ROOT / slug
        out.mkdir(parents=True, exist_ok=True)
        if not chain_only and not (out / "nominate.txt").is_file():
            pending.append(("nominate", slug, target, disease, as_of))
        if chain_only or not (out / "chain.txt").is_file():
            pending.append(("chain", slug, target, disease, as_of))
    print(f"pending {len(pending)}", flush=True)
    failed = []
    texts: dict[tuple[str, str], str] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(_job, kind, slug, target, disease, as_of, key)
            for kind, slug, target, disease, as_of in pending
        ]
        for fut in as_completed(futures):
            kind = ""
            slug = ""
            try:
                slug, kind, text = fut.result()
                texts[(slug, kind)] = text
                (ROOT / slug / f"{kind}.txt").write_text(text.strip() + "\n", encoding="utf-8")
            except Exception as exc:
                failed.append(f"{slug}:{kind}")
                print(f"fail {_safe(exc, key)}", flush=True)
    for slug, target, disease, as_of in PAIRS:
        out = ROOT / slug
        if not (out / "nominate.txt").is_file() or not (out / "chain.txt").is_file():
            continue
        record = {
            "model": MODEL,
            "provider": "volcengine-coding-plan",
            "temperature": 0,
            "thinking": "enabled",
            "target": target,
            "disease": disease,
            "as_of": as_of,
            "called_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nominate_prompt": NOMINATE.format(target=target, as_of=as_of).strip(),
            "chain_prompt": CHAIN_PROMPT.format(target=target, disease=disease, as_of=as_of).strip(),
            "nominate_file": "nominate.txt",
            "chain_file": "chain.txt",
            "host": "bohrium-pairs",
        }
        (out / "calls.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {slug}", flush=True)
    if failed:
        raise SystemExit("failed " + ",".join(failed))


if __name__ == "__main__":
    main()
