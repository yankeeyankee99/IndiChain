"""Ordinary-model calls for C5 × Neuromyelitis Optica.

One deepseek-v4-pro call each, temperature 0, thinking enabled.
Runs beside ordinary_rest.py. Does not write the key.
"""

from __future__ import annotations

import json
import os
import ssl
import time
from datetime import datetime, timezone
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path("/root/ordinary-rest/out")
ENV = Path("/root/ordinary-rest/env.txt")
MODEL = "deepseek-v4-pro"
GATEWAY = "https://open.bohrium.com/openapi/model-gateway/v1/chat/completions"
GATEWAY_PRO = "deepseek-v4-pro-0813"
CODING = "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"

PAIRS = (("c5-nmo", "C5", "Neuromyelitis Optica", "2009-04-01"),)

NOMINATE = """Target: {target}. Use only information available on or before {as_of}. Treat later approvals, trials, reviews, and findings as not yet available.

Find new therapeutic indications for this target. List 5 specific diseases, ordered from the one you would test first to the one you would test fifth. Use English disease names. Put each disease on its own line:

1. Disease name
2. Disease name
3. Disease name
4. Disease name
5. Disease name
"""

CHAIN_PROMPT = """Target: {target}. Disease: {disease}. Use only information available on or before {as_of}. Treat later approvals, trials, reviews, and findings as not yet available.

Write every mechanistic path you consider possible from this target to this disease. Start each path at the target and end it at the disease. Each step moves from one entity to the next. For each step, give one sentence from the literature and its citation. If a step has no literature, write none. Use English. Put each step on its own line, and separate paths with a blank line:

Left → Right | Sentence | Citation or none
"""


def _load_env(path: Path) -> None:
    text = path.read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _http_json(url: str, data: bytes, headers: dict[str, str], timeout: int) -> dict:
    req = Request(url, data=data, headers=headers)
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
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
        time.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last


def _target() -> tuple[str, str, str]:
    access = (os.environ.get("Accesskey") or os.environ.get("BOHR_ACCESS_KEY") or "").strip()
    if access:
        return GATEWAY, access, GATEWAY_PRO
    key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("LLM calls need ARK_API_KEY")
    return CODING, key, MODEL


def _call(prompt: str, timeout: int) -> str:
    url, key, sent = _target()
    payload = {
        "model": sent,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "thinking": {"type": "enabled"},
    }
    body = _http_json(
        url,
        json.dumps(payload).encode(),
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout,
    )
    return str(body["choices"][0]["message"].get("content") or "")


def _safe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for name in ("ARK_API_KEY", "Accesskey", "BOHR_ACCESS_KEY"):
        secret = os.environ.get(name) or ""
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text[:400]


def _one(slug: str, target: str, disease: str, as_of: str) -> None:
    out = ROOT / slug
    out.mkdir(parents=True, exist_ok=True)
    if (out / "calls.json").exists():
        print(f"skip {slug}", flush=True)
        return
    nominate_text = NOMINATE.format(target=target, as_of=as_of)
    chain_text = CHAIN_PROMPT.format(target=target, disease=disease, as_of=as_of)
    print(f"nominate {slug}", flush=True)
    nominate = _call(nominate_text, 900)
    (out / "nominate.txt").write_text(nominate.strip() + "\n", encoding="utf-8")
    print(f"chain {slug}", flush=True)
    chain = _call(chain_text, 1500)
    (out / "chain.txt").write_text(chain.strip() + "\n", encoding="utf-8")
    record = {
        "model": MODEL,
        "temperature": 0,
        "thinking": "enabled",
        "target": target,
        "disease": disease,
        "as_of": as_of,
        "called_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nominate_prompt": nominate_text.strip(),
        "chain_prompt": chain_text.strip(),
        "nominate_file": "nominate.txt",
        "chain_file": "chain.txt",
        "host": "bohrium-pairs",
    }
    (out / "calls.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {slug}", flush=True)


def main() -> None:
    _load_env(ENV)
    failed = []
    for slug, target, disease, as_of in PAIRS:
        try:
            _one(slug, target, disease, as_of)
        except Exception as exc:
            failed.append(slug)
            print(f"fail {slug} {_safe(exc)}", flush=True)
    if failed:
        raise SystemExit("failed " + ",".join(failed))


if __name__ == "__main__":
    main()
