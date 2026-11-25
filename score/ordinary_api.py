"""Ordinary-model nomination and chain calls for the eight scored pairs.

One deepseek-v4-pro call each, temperature 0, thinking enabled. Reads
ARK_API_KEY or Accesskey from the nominate env file. Does not write the key.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CHAIN = Path(__file__).resolve().parents[1] / "chain"
sys.path.insert(0, str(CHAIN))

from run import MODEL_PRO, _deepseek, _load_dotenv  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "打分api普通模型"
ENV = Path(__file__).resolve().parents[1] / "nominate" / ".env.txt"

# The seven nominate pairs that already have a chain, plus Kv1.3 × IgA nephropathy.
PAIRS = (
    ("kv1.3-igan", "Kv1.3", "IgA nephropathy", "2026-04-30"),
    ("il17a-hidradenitis", "IL17A", "Hidradenitis Suppurativa", "2016-07-11"),
    ("il4r-eoe", "IL4R", "Eosinophilic Esophagitis", "2015-05-12"),
    ("il4r-bullous", "IL4R", "Pemphigoid, Bullous", "2020-10-28"),
    ("cfb-igan", "CFB", "Glomerulonephritis, IGA", "2018-02-07"),
    ("c5-mg", "C5", "Myasthenia Gravis", "2008-10-01"),
    ("il5ra-churg-strauss", "IL5RA", "Churg-Strauss Syndrome", "2017-04-15"),
    ("tslp-nasal-polyps", "TSLP", "Nasal Polyps", "2021-04-22"),
)

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


def nominate_prompt(target: str, as_of: str) -> str:
    return NOMINATE.format(target=target, as_of=as_of)


def chain_prompt(target: str, disease: str, as_of: str) -> str:
    return CHAIN_PROMPT.format(target=target, disease=disease, as_of=as_of)


def _check() -> None:
    text = chain_prompt("Kv1.3", "IgA nephropathy", "2026-04-30")
    assert "every mechanistic path you consider possible" in text
    assert "Write one mechanistic path" not in text
    assert "separate paths with a blank line" in text
    assert "Left → Right | Sentence | Citation or none" in text
    assert "Target: IL17A." in nominate_prompt("IL17A", "2016-07-11")
    assert len(PAIRS) == 8
    assert PAIRS[0][0] == "kv1.3-igan"
    print("check ok", flush=True)


def _load_env_file(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _safe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for name in ("ARK_API_KEY", "Accesskey", "BOHR_ACCESS_KEY"):
        secret = os.environ.get(name) or ""
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text[:400]


def _one(slug: str, target: str, disease: str, as_of: str) -> None:
    out = ROOT / slug / "普通模型"
    out.mkdir(parents=True, exist_ok=True)
    nominate_text = nominate_prompt(target, as_of)
    chain_text = chain_prompt(target, disease, as_of)
    print(f"nominate {slug}", flush=True)
    nominate = _deepseek(nominate_text, model=MODEL_PRO, timeout=900)
    (out / "nominate.txt").write_text(nominate.strip() + "\n", encoding="utf-8")
    print(f"chain {slug}", flush=True)
    chain = _deepseek(chain_text, model=MODEL_PRO, timeout=1500)
    (out / "chain.txt").write_text(chain.strip() + "\n", encoding="utf-8")
    record = {
        "model": MODEL_PRO,
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
    }
    (out / "calls.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {slug}", flush=True)


def main() -> None:
    _load_dotenv()
    _load_env_file(ENV)
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
    if "--check" in sys.argv:
        _check()
    else:
        main()
