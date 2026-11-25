"""One Coding Plan call of an English prompt file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPT_PATH = ROOT / "prompt4.md"
ENV_PATH = ROOT / ".env.txt"
BASE = "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"
GATEWAY = "https://open.bohrium.com/openapi/model-gateway/v1/chat/completions"
GATEWAY_MODEL = "deepseek-v4-pro-0813"
KEY_NAMES = ("ARK_API_KEY", "TIDE_LLM_API_KEY")
THINKING_TYPES = {"enabled", "disabled", "auto"}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
LISTS_MARK = "<<<CLOSED_LISTS>>>"
CITE = re.compile(
    r"\b(Action|Actions|Process|Processes|Product|Products)\s+(\d+)\b",
    re.I,
)
CANON = {
    "action": "Action",
    "actions": "Action",
    "process": "Process",
    "processes": "Process",
    "product": "Product",
    "products": "Product",
}


def load_key() -> str:
    for name in KEY_NAMES:
        value = os.environ.get(name, "").strip().strip('"').strip("'")
        if value:
            return value
    if not ENV_PATH.is_file():
        raise RuntimeError(f"missing {ENV_PATH}")
    found: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.replace("export", "").strip()
        value = value.strip().strip('"').strip("'")
        if name in KEY_NAMES and value:
            found[name] = value
    for name in KEY_NAMES:
        if name in found:
            return found[name]
    raise RuntimeError("ARK_API_KEY missing in .env.txt")


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        joined = "".join(parts).strip()
        if joined:
            return joined
    for key in ("reasoning_content", "reasoning"):
        extra = message.get(key)
        if isinstance(extra, str) and extra.strip():
            return extra
    return ""


def read_sse(resp) -> dict:
    content: list[str] = []
    reasoning: list[str] = []
    usage = None
    finish = None
    model_returned = None
    seen = 0
    read_sse._bucket = 0
    while True:
        line = resp.readline()
        if not line:
            break
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            break
        event = json.loads(data)
        model_returned = event.get("model") or model_returned
        if event.get("usage"):
            usage = event["usage"]
        for choice in event.get("choices") or []:
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            piece = delta.get("content") or ""
            thought = delta.get("reasoning_content") or ""
            if piece:
                content.append(piece)
                if len(content) == 1:
                    print("answer_started", flush=True)
            if thought:
                reasoning.append(thought)
            seen += len(piece) + len(thought)
            bucket = seen // 8000
            if bucket and bucket != getattr(read_sse, "_bucket", 0):
                read_sse._bucket = bucket
                print(f"stream_chars {seen}", flush=True)
    return {
        "model": model_returned,
        "usage": usage,
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": "".join(content),
                    "reasoning_content": "".join(reasoning),
                },
            }
        ],
    }


def extract_lists(content: str) -> str | None:
    start = content.find("## Actions")
    if start < 0:
        return None
    block = content[start:].strip()
    if "## Processes" not in block or "## Products" not in block:
        return None
    return block


def item_labels(lists: str) -> set[str]:
    labels: set[str] = set()
    section = ""
    for line in lists.splitlines():
        if line.startswith("## Actions"):
            section = "Action"
        elif line.startswith("## Processes"):
            section = "Process"
        elif line.startswith("## Products"):
            section = "Product"
        match = re.match(r"(\d+)\.\s+", line.strip())
        if section and match:
            labels.add(f"{section} {match.group(1)}")
    return labels


def cited_labels(row: str) -> list[str]:
    found = []
    for kind, number in CITE.findall(row):
        found.append(f"{CANON[kind.lower()]} {number}")
    return found


def item_phrases(lists: str) -> list[str]:
    phrases = []
    for line in lists.splitlines():
        match = re.match(r"\d+\.\s+(.+)", line.strip())
        if not match:
            continue
        text = match.group(1).strip().rstrip(".")
        if len(text) >= 8:
            phrases.append(text.lower())
    return phrases


def part_a_rows(content: str, lists: str) -> tuple[list[str], list[str]]:
    labels = item_labels(lists)
    phrases = item_phrases(lists)
    lines = content.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("|")), None)
    if start is None:
        return [], []
    rows: list[str] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    if len(rows) < 3:
        return [], []
    counted: list[str] = []
    outside: list[str] = []
    for line in rows[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: "):
            continue
        name = cells[0]
        cites = cited_labels(line)
        numbered_ok = bool(cites) and all(cite in labels for cite in cites)
        quoted_ok = any(phrase in line.lower() for phrase in phrases)
        headers = [cell.strip().casefold() for cell in rows[0].strip().strip("|").split("|")]
        name_only = "mesh" in headers and "action" not in headers
        if name_only or numbered_ok or (not cites and quoted_ok):
            counted.append(name)
        else:
            outside.append(name)
    return counted, outside


def self_check() -> None:
    sample = "## Actions\n1. Calcium entry.\n## Processes\n1. Recruitment.\n## Products\n1. Cytokines.\n"
    assert extract_lists("preamble\n" + sample) == sample.strip()
    assert extract_lists("no headings") is None
    assert item_labels(sample) == {"Action 1", "Process 1", "Product 1"}
    table = "\n".join(
        [
            "| Disease | process |",
            "|---|---|",
            "| Lupus | Process 9 |",
            "| Crohn | Process 1 |",
            "| Celiac | Processes 1 |",
            "| Mixed | Process 1 and Product 9 |",
            "| HS | Recruitment of leukocytes |",
        ]
    )
    counted, outside = part_a_rows(table, sample)
    assert counted == ["Crohn", "Celiac", "HS"]
    assert outside == ["Lupus", "Mixed"]
    mesh_table = "\n".join(
        [
            "| Disease | MeSH |",
            "|---|---|",
            "| Pemphigus vulgaris | D010392 |",
        ]
    )
    counted, outside = part_a_rows(mesh_table, sample)
    assert counted == ["Pemphigus vulgaris"] and outside == []
    bare = {"function": {"arguments": '{"as_of": 2026-04-30, "intervention": "Kv1.3"}'}}
    assert _tool_args(bare) == {"as_of": "2026-04-30", "intervention": "Kv1.3"}
    assert _started_before("2019-01-30", "2019-01-31")
    assert not _started_before("2019-01-31", "2019-01-31")
    assert not _started_before("2019-01", "2019-01-31")
    assert _started_before("2018-12", "2019-01-31")
    assert _started_before("2018", "2019-01-31")
    assert not _started_before("2019", "2019-01-31")
    assert not _started_before("", "2019-01-31")
    assert _day_before("2019-01-31") == "2019-01-30"
    study = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT03713619", "briefTitle": "HS"},
            "statusModule": {
                "startDateStruct": {"date": "2019-01-31"},
                "overallStatus": "COMPLETED",
            },
            "conditionsModule": {"conditions": ["Hidradenitis Suppurativa"]},
            "armsInterventionsModule": {"interventions": [{"name": "Secukinumab"}]},
        }
    }
    row = _ct_study(study)
    assert row["nct_id"] == "NCT03713619" and row["start"] == "2019-01-31"
    assert not _started_before(row["start"], "2019-01-31")
    kept = prepare_judge_prompt(KV_CONTEXT + "\n\n" + DISEASE_CLASS_CLOSED)
    assert "non-neoplastic disorders of the nervous system" not in kept
    assert "primary tumor" in kept
    opened = prepare_judge_prompt("States that depend on IL17A.\n\n" + DISEASE_CLASS_CLOSED)
    assert "non-neoplastic disorders of the nervous system" not in opened
    assert "primary tumor" in opened
    prompt4 = (ROOT / "prompt4.md").read_text(encoding="utf-8")
    prior = (ROOT / "prompt4_prior.md").read_text(encoding="utf-8")
    context = (ROOT / "prompt_context.md").read_text(encoding="utf-8")
    assert "clinical_trials" in prompt4 and "clinical_trials" not in prior
    assert "call mesh" in prompt4 and "call mesh" in prior
    assert _prompt_tools(prior) == [MESH_TOOL]
    assert _prompt_tools(prompt4) == [CLINICAL_TOOL, MESH_TOOL]
    assert "New indications" not in prior
    start = prompt4.find("\nNew indications. ")
    end = prompt4.find("\nDisease class. ", start)
    assert start > 0 and end > start
    assert prompt4[:start] + prompt4[end:] == prior
    for text in (prompt4, prior):
        assert "known to injure" not in text
        assert "MeSH preferred term" not in text
        assert "absent recalled evidence" not in text
        assert "Citations are not required" not in text
    assert "cannot recall" not in prompt4
    assert "also acts on the target" not in prompt4
    assert DISEASE_CLASS in prompt4 and DISEASE_CLASS in prior
    assert "non-neoplastic disorders of the nervous system" not in prompt4
    assert "non-neoplastic disorders of the nervous system" not in prior
    assert "easy to overlook" not in context and "another cell" not in context
    assert "<<<TARGET>>>" in context
    _cache_self_check()


def post(body: dict, stream: bool) -> tuple[dict | None, str, float]:
    req = urllib.request.Request(
def _env_file() -> dict[str, str]:
    found: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return found
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.replace("export", "").strip()
        value = value.strip().strip('"').strip("'")
        if name and value:
            found[name] = value
    return found


def chat_url(model: str) -> str:
    return GATEWAY if model == GATEWAY_MODEL else BASE


def chat_auth(model: str) -> tuple[str, str]:
    """Return the bearer token and the provider name stored on the call record."""
    if model == GATEWAY_MODEL:
        key = (
            os.environ.get("Accesskey", "").strip()
            or os.environ.get("BOHR_ACCESS_KEY", "").strip()
            or _env_file().get("Accesskey", "")
        )
        if not key:
            raise RuntimeError("Accesskey missing in .env.txt")
        return key, "bohrium-model-gateway"
    return load_key(), "volcengine-coding-plan"


def post(body: dict, stream: bool) -> tuple[dict | None, str, float]:
    model = str(body.get("model") or "")
    token, _provider = chat_auth(model)
    req = urllib.request.Request(
        chat_url(model),
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
        method="POST",
    )
    started = time.time()
    last_error = ""
    payload = None
    for attempt in range(1, 4):
        try:
            socket_timeout = 600 if stream else 3600
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                if stream:
                    payload = read_sse(resp)
                else:
                    payload = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.read().decode()[:500]}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2 * attempt)
    return payload, last_error, round(time.time() - started, 3)


CT_STUDIES = "https://clinicaltrials.gov/api/v2/studies"
MAX_TOOL_ROUNDS = 8
CACHE_FORMAT = "indicator-http-cache"
CACHE_VERSION = 1
CACHE_ENV = "INDICATOR_HTTP_CACHE"
_CACHE_DIR = {"mesh": "mesh", "clinical_trials": "clinical-trials"}
KV_CONTEXT = (
    "Class-switched memory B cells, plasmablasts, and antibody-secreting cells depend on Kv1.3"
)
DISEASE_CLASS_CLOSED = (
    "Disease class. The kept indications are diseases whose injury follows the mechanism read from the three lists "
    "and whose accepted class is outside non-neoplastic disorders of the nervous system. "
    "Classify each disease by its accepted class. "
    "A nervous-system disease stays outside the kept indications when its mechanism is read through an immune cell. "
    "A primary tumor whose injury follows the listed mechanism remains a kept indication."
)
DISEASE_CLASS = (
    "Disease class. Classify each disease by its accepted class. "
    "A primary tumor whose injury follows the listed mechanism remains a kept indication."
)
CLINICAL_TOOL = {
    "type": "function",
    "function": {
        "name": "clinical_trials",
        "description": (
            "Search the public ClinicalTrials.gov studies API. "
            "Returns trials whose start date is strictly before as_of. "
            "A trial that starts on as_of is omitted. "
            "An empty trials list means this search found no earlier trial. "
            "An error is not an empty result. "
            "Use it for a target, a drug, or both, with or without a disease."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "as_of": {"type": "string", "description": "Freeze date, YYYY-MM-DD."},
                "condition": {
                    "type": "string",
                    "description": "Disease name. Optional when the search is only for a drug or a target.",
                },
                "intervention": {
                    "type": "string",
                    "description": "Drug, biologic, or target name.",
                },
            },
            "required": ["as_of"],
        },
    },
}
MESH_TOOL = {
    "type": "function",
    "function": {
        "name": "mesh",
        "description": (
            "Look up one disease name in the public MeSH vocabulary. "
            "Returns a descriptor only when that name is its preferred heading or an entry term. "
            "An empty descriptors list means this name is not a MeSH term. "
            "An error is not an empty result. "
            "Use the returned preferred label as the disease name and the returned descriptor ID. "
            "Do not substitute a remembered ID."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Disease name to match."},
            },
            "required": ["name"],
        },
    },
}
TOOLS = [CLINICAL_TOOL, MESH_TOOL]


def _started_before(start: str, as_of: str) -> bool:
    s = (start or "").strip()
    a = (as_of or "").strip()
    if len(s) not in (4, 7, 10) or len(a) < 10:
        return False
    return s < a[: len(s)]


def _day_before(as_of: str) -> str:
    year, month, day = (int(part) for part in as_of.split("-"))
    return (date(year, month, day) - timedelta(days=1)).isoformat()


def _ct_study(study: dict) -> dict:
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    status = proto.get("statusModule") or {}
    arms = proto.get("armsInterventionsModule") or {}
    conds = proto.get("conditionsModule") or {}
    interventions = [
        item.get("name")
        for item in (arms.get("interventions") or [])
        if item.get("name")
    ]
    return {
        "nct_id": ident.get("nctId") or "",
        "title": ident.get("briefTitle") or "",
        "start": (status.get("startDateStruct") or {}).get("date") or "",
        "status": status.get("overallStatus") or "",
        "interventions": interventions,
        "conditions": list(conds.get("conditions") or []),
    }


def _ct_get(params: dict) -> dict:
    url = CT_STUDIES + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "indicator-nominate", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cache_root() -> Path | None:
    raw = os.environ.get(CACHE_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


def _cache_digest(key: list) -> str:
    blob = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _response_ok(kind: str, key: list, response: object) -> bool:
    if not isinstance(response, dict) or "error" in response:
        return False
    if kind == "mesh":
        return isinstance(response.get("descriptors"), list) and response.get("name") == key[0]
    if kind == "clinical_trials":
        return (
            isinstance(response.get("trials"), list)
            and response.get("as_of") == key[0]
            and response.get("condition") == key[1]
            and response.get("intervention") == key[2]
        )
    return False


def _cache_file(kind: str, key: list) -> Path | None:
    root = _cache_root()
    if root is None:
        return None
    return root / _CACHE_DIR[kind] / f"{_cache_digest(key)}.json"


def _cache_load(path: Path, kind: str, key: list) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("format") != CACHE_FORMAT or data.get("version") != CACHE_VERSION:
        return None
    if data.get("kind") != kind or data.get("key") != key:
        return None
    response = data.get("response")
    if not _response_ok(kind, key, response):
        return None
    return response


def _cache_read(kind: str, key: list) -> dict | None:
    path = _cache_file(kind, key)
    if path is None or not path.is_file():
        return None
    return _cache_load(path, kind, key)


def _cache_write(kind: str, key: list, response: dict) -> None:
    if not _response_ok(kind, key, response):
        return
    path = _cache_file(kind, key)
    if path is None:
        return
    try:
        if path.is_file() and _cache_load(path, kind, key) is not None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (
            json.dumps(
                {
                    "format": CACHE_FORMAT,
                    "version": CACHE_VERSION,
                    "kind": kind,
                    "key": key,
                    "response": response,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        try:
            if path.is_file() and _cache_load(path, kind, key) is not None:
                return
            if path.exists():
                os.replace(tmp, path)
                return
            try:
                os.link(tmp, path)
            except FileExistsError:
                if _cache_load(path, kind, key) is None:
                    os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        return


def _cache_self_check() -> None:
    import tempfile

    previous = os.environ.get(CACHE_ENV)
    with tempfile.TemporaryDirectory() as folder:
        os.environ[CACHE_ENV] = folder
        try:
            _cache_write("mesh", ["Heart Failure"], {"error": "down", "descriptors": []})
            assert _cache_read("mesh", ["Heart Failure"]) is None
            empty = {"name": "Heart Failure", "descriptors": []}
            _cache_write("mesh", ["Heart Failure"], empty)
            assert _cache_read("mesh", ["Heart Failure"]) == empty
            assert _cache_read("mesh", ["heart failure"]) is None
            saved = _cache_file("mesh", ["Heart Failure"])
            assert saved is not None and b"\r" not in saved.read_bytes()
            assert mesh_lookup("  Heart Failure  ") == empty
            path = _cache_file("mesh", ["Heart Failure"])
            assert path is not None
            path.write_bytes(
                json.dumps(
                    {
                        "format": CACHE_FORMAT,
                        "version": CACHE_VERSION,
                        "kind": "mesh",
                        "key": ["Asthma"],
                        "response": {"name": "Asthma", "descriptors": []},
                    }
                ).encode()
            )
            assert _cache_read("mesh", ["Heart Failure"]) is None
            path.write_bytes(b"{")
            assert _cache_read("mesh", ["Heart Failure"]) is None
            _cache_write("mesh", ["Heart Failure"], empty)
            assert _cache_read("mesh", ["Heart Failure"]) == empty
            trials = {
                "as_of": "2017-03-01",
                "condition": "a|b",
                "intervention": "",
                "trials": [],
            }
            _cache_write("clinical_trials", ["2017-03-01", "a|b", ""], trials)
            assert clinical_trials("2017-03-01", "a|b", "") == trials
            assert _cache_read("clinical_trials", ["2017-03-02", "a|b", ""]) is None
            assert _cache_read("clinical_trials", ["2017-03-01", "a", "b"]) is None
            assert "error" in clinical_trials("2017/03/01", "a", "b")
            assert _cache_read("clinical_trials", ["2017/03/01", "a", "b"]) is None
            blocked = Path(folder) / "blocked"
            blocked.write_text("x", encoding="utf-8")
            os.environ[CACHE_ENV] = str(blocked / "v1")
            _cache_write("mesh", ["Heart Failure"], empty)
            assert _cache_read("mesh", ["Heart Failure"]) is None
        finally:
            if previous is None:
                os.environ.pop(CACHE_ENV, None)
            else:
                os.environ[CACHE_ENV] = previous


def clinical_trials(as_of: str, condition: str = "", intervention: str = "") -> dict:
    as_of = (as_of or "").strip()
    condition = (condition or "").strip()
    intervention = (intervention or "").strip()
    if not condition and not intervention:
        return {"error": "Provide a condition, an intervention, or both.", "trials": []}
    try:
        end = _day_before(as_of)
    except (TypeError, ValueError):
        return {"error": "as_of must be YYYY-MM-DD.", "trials": []}
    cache_key = [as_of, condition, intervention]
    cached = _cache_read("clinical_trials", cache_key)
    if cached is not None:
        return cached
    params = {
        "filter.advanced": f"AREA[StartDate]RANGE[MIN,{end}]",
        "pageSize": "20",
        "countTotal": "true",
        "fields": "NCTId,BriefTitle,StartDate,OverallStatus,InterventionName,Condition",
    }
    if condition:
        params["query.cond"] = condition
    if intervention:
        params["query.intr"] = intervention
    trials: list[dict] = []
    # ponytail: three pages of twenty studies; a later page can still hold a pre-freeze trial. Upgrade path: follow nextPageToken until the page is empty.
    for _ in range(3):
        try:
            payload = _ct_get(params)
        except (OSError, json.JSONDecodeError) as exc:
            return {"error": str(exc), "trials": []}
        for study in payload.get("studies") or []:
            row = _ct_study(study)
            if _started_before(row["start"], as_of):
                trials.append(row)
            if len(trials) == 15:
                break
        token = payload.get("nextPageToken")
        if len(trials) == 15 or not token:
            break
        params["pageToken"] = token
    result = {
        "as_of": as_of,
        "condition": condition,
        "intervention": intervention,
        "trials": trials,
    }
    _cache_write("clinical_trials", cache_key, result)
    return result


def _mesh_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "indicator-nominate", "Accept": "application/json"},
    )
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            last = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    assert last is not None
    raise last


def _mesh_rows(kind: str, label: str) -> list[dict]:
    query = urllib.parse.urlencode({"label": label, "match": "exact", "limit": "5"})
    data = _mesh_json(f"https://id.nlm.nih.gov/mesh/lookup/{kind}?{query}")
    rows = []
    for row in data if isinstance(data, list) else []:
        tail = str(row.get("resource") or "").rstrip("/").split("/")[-1]
        text = str(row.get("label") or "").strip()
        if tail and text:
            rows.append({"id": tail, "label": text})
    return rows


def _mesh_from_term(term_id: str) -> list[dict]:
    query = (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#>\n"
        "SELECT ?id ?label WHERE {\n"
        f"  ?c meshv:term <http://id.nlm.nih.gov/mesh/{term_id}> .\n"
        "  ?d meshv:preferredConcept ?c .\n"
        "  ?d meshv:identifier ?id .\n"
        "  ?d rdfs:label ?label .\n"
        "}"
    )
    url = "https://id.nlm.nih.gov/mesh/sparql?" + urllib.parse.urlencode(
        {"query": query, "format": "JSON", "inference": "false"}
    )
    data = _mesh_json(url)
    found = []
    for row in data.get("results", {}).get("bindings") or []:
        mesh = str((row.get("id") or {}).get("value") or "")
        label = str((row.get("label") or {}).get("value") or "").strip()
        if mesh.startswith("D") and label:
            found.append({"mesh": mesh, "preferred_label": label})
    return found


def _mesh_same(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


def mesh_lookup(name: str) -> dict:
    # ponytail: exact heading or entry term only. A near spelling returns nothing; the model tries the official name.
    name = " ".join((name or "").split())
    if not name:
        return {"error": "Provide a disease name.", "descriptors": []}
    cache_key = [name]
    cached = _cache_read("mesh", cache_key)
    if cached is not None:
        return cached
    try:
        found = []
        for row in _mesh_rows("descriptor", name):
            if row["id"].startswith("D") and _mesh_same(name, row["label"]):
                found.append({
                    "mesh": row["id"],
                    "preferred_label": row["label"],
                    "matched": row["label"],
                })
        if not found:
            for row in _mesh_rows("term", name):
                if not row["id"].startswith("T") or not _mesh_same(name, row["label"]):
                    continue
                for desc in _mesh_from_term(row["id"]):
                    found.append({**desc, "matched": row["label"]})
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": str(exc), "descriptors": []}
    descriptors = []
    seen: set[str] = set()
    for row in found:
        if row["mesh"] in seen:
            continue
        seen.add(row["mesh"])
        descriptors.append(row)
    result = {"name": name, "descriptors": descriptors}
    _cache_write("mesh", cache_key, result)
    return result


def _prompt_tools(prompt: str) -> list:
    selected = []
    if "clinical_trials" in prompt:
        selected.append(CLINICAL_TOOL)
    if "call mesh" in prompt:
        selected.append(MESH_TOOL)
    return selected


def prepare_judge_prompt(prompt: str) -> str:
    return prompt.replace(DISEASE_CLASS_CLOSED, DISEASE_CLASS)


def _tool_args(call: dict) -> dict | None:
    raw = ((call.get("function") or {}).get("arguments")) or "{}"
    if isinstance(raw, dict):
        args = raw
    else:
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            fixed = re.sub(r"(:\s*)(\d{4}-\d{2}-\d{2})(\s*[,}])", r'\1"\2"\3', raw)
            try:
                args = json.loads(fixed)
            except json.JSONDecodeError:
                return None
    if not isinstance(args, dict):
        return None
    if args.get("as_of") is not None:
        args["as_of"] = str(args["as_of"])
    return args


def _run_tool(call: dict) -> dict:
    name = (call.get("function") or {}).get("name") or ""
    args = _tool_args(call)
    if args is None:
        return {"error": "Tool arguments are not a JSON object."}
    if name == "clinical_trials":
        return clinical_trials(
            str(args.get("as_of", "")),
            str(args.get("condition", "") or ""),
            str(args.get("intervention", "") or ""),
        )
    if name == "mesh":
        return mesh_lookup(str(args.get("name") or ""))
    return {"error": f"Unknown tool {name}."}


def _assistant_message(message: dict) -> dict:
    assistant = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("reasoning_content"):
        assistant["reasoning_content"] = message["reasoning_content"]
    if message.get("tool_calls"):
        assistant["tool_calls"] = message["tool_calls"]
    return assistant


def request_body(
    model: str,
    prompt: str,
    max_tokens: int,
    stream: bool,
    thinking: str | None,
    effort: str | None,
    tools: bool = False,
    messages: list | None = None,
    tool_defs: list | None = None,
) -> dict:
    body = {
        "model": model,
        "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if thinking:
        body["thinking"] = {"type": thinking}
    if effort:
        body["reasoning_effort"] = effort
    chosen = tool_defs if tool_defs is not None else (TOOLS if tools else None)
    if chosen:
        body["tools"] = chosen
        body["tool_choice"] = "auto"
    elif stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


def write_record(raw_dir: Path, stem: str, record: dict, content: str, reasoning: str) -> None:
    (raw_dir / f"{stem}.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (raw_dir / f"{stem}.md").write_text(content or "", encoding="utf-8")
    (raw_dir / f"{stem}.reasoning.md").write_text(reasoning or "", encoding="utf-8")


def message_parts(payload: dict) -> tuple[str, str, str]:
    choice = payload["choices"][0]
    message = choice.get("message") or {}
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    reasoning = message.get("reasoning_content") if isinstance(message.get("reasoning_content"), str) else ""
    finish = choice.get("finish_reason") or ""
    return content or "", reasoning or "", finish


def main() -> None:
    if "--self-check" in sys.argv:
        self_check()
        print("self_check_ok")
        return
    model = sys.argv[1]
    stream = "--stream" in sys.argv
    def flag(name: str, default: str | None = None) -> str | None:
        if name in sys.argv:
            return sys.argv[sys.argv.index(name) + 1]
        return default

    run_idx = int(flag("--run", "1") or "1")
    tag = flag("--tag", "K-EN") or "K-EN"
    prompt_path = Path(flag("--prompt", str(PROMPT_PATH)) or PROMPT_PATH)
    thinking = flag("--thinking")
    effort = flag("--reasoning-effort")
    if thinking and thinking not in THINKING_TYPES:
        raise SystemExit(f"unsupported thinking type: {thinking}")
    if effort and effort not in EFFORTS:
        raise SystemExit(f"unsupported reasoning_effort: {effort}")
    skip = set()
    for name in ("--run", "--tag", "--prompt", "--thinking", "--reasoning-effort", "--out"):
        if name in sys.argv:
            skip.add(sys.argv.index(name) + 1)
    numbers = [
        arg for i, arg in enumerate(sys.argv) if i > 1 and i not in skip and arg.isdigit()
    ]
    max_tokens = int(numbers[0]) if numbers else 32000
    out = flag("--out")
    raw_dir = Path(out) if out else (ROOT / "data" / f"run{run_idx}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{tag}_{model}_run{run_idx}"
    if (raw_dir / f"{stem}.jsonl").exists() or (raw_dir / f"{stem}_lists.jsonl").exists():
        raise SystemExit(f"refusing to overwrite {stem}")
    if not prompt_path.is_file():
        raise SystemExit(f"prompt file not found: {prompt_path}")
    judge_template = prompt_path.read_text(encoding="utf-8")
    two_step = LISTS_MARK in judge_template
    lists_path = prompt_path.with_name(f"{prompt_path.stem}_lists.md")
    if two_step and not lists_path.is_file():
        raise SystemExit(f"prompt file not found: {lists_path}")

    def run_once(prompt: str, out_stem: str, prompt_file: Path) -> tuple[dict | None, str, str, str]:
        tool_defs = _prompt_tools(prompt)
        use_tools = bool(tool_defs)
        messages: list[dict] = [{"role": "user", "content": prompt}]
        tool_trace: list[dict] = []
        payload = None
        last_error = ""
        elapsed = 0.0
        content, reasoning, finish = "", "", ""
        body: dict = {}
        rounds = MAX_TOOL_ROUNDS if use_tools else 1
        for i in range(rounds):
            body = request_body(
                model, prompt, max_tokens, stream and not use_tools, thinking, effort,
                tools=use_tools, messages=messages, tool_defs=tool_defs,
            )
            payload, last_error, step_elapsed = post(body, stream and not use_tools)
            elapsed += step_elapsed
            if payload is None:
                break
            content, reasoning, finish = message_parts(payload)
            message = (payload.get("choices") or [{}])[0].get("message") or {}
            calls = message.get("tool_calls") or []
            if not use_tools or not calls:
                break
            messages.append(_assistant_message(message))
            for call in calls:
                result = _run_tool(call)
                name = (call.get("function") or {}).get("name") or ""
                tool_trace.append({
                    "name": name,
                    "arguments": (call.get("function") or {}).get("arguments"),
                    "result": result,
                })
                print(
                    json.dumps(
                        {
                            "tool": name,
                            "n": len(result.get("trials") or result.get("descriptors") or []),
                            "error": result.get("error", ""),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": json.dumps(result, ensure_ascii=False),
                })
            if i == rounds - 1:
                messages.append({
                    "role": "user",
                    "content": (
                        "Write the indication table now from the tool results already returned. "
                        "Do not call tools again."
                    ),
                })
                body = request_body(
                    model, prompt, max_tokens, False, thinking, effort,
                    tools=False, messages=messages,
                )
                payload, last_error, step_elapsed = post(body, False)
                elapsed += step_elapsed
                if payload:
                    content, reasoning, finish = message_parts(payload)
                break
        elapsed = round(elapsed, 3)
        if payload is not None and not content.strip():
            raise SystemExit(last_error or "model returned an empty indication table")
        record = {
            "condition": tag,
            "prompt_path": str(prompt_file),
            "provider": "volcengine-coding-plan",
            "model": model,
            "provider": chat_auth(model)[1],
            "pilot": True,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "request": body,
            "tool_rounds": len(tool_trace),
            "tools": tool_trace,
            "elapsed_seconds": elapsed,
            "error": last_error if payload is None else "",
            "response": payload,
        }
        write_record(raw_dir, out_stem, record, content, reasoning)
        print(
            json.dumps(
                {
                    "step": out_stem,
                    "thinking": thinking or "",
                    "reasoning_effort": effort or "",
                    "model_returned": (payload or {}).get("model"),
                    "finish_reason": finish,
                    "elapsed_seconds": elapsed,
                    "usage": (payload or {}).get("usage"),
                    "content_chars": len(content),
                    "reasoning_chars": len(reasoning),
                    "text_path": str(raw_dir / f"{out_stem}.md"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if payload is None:
            raise SystemExit(last_error)
        return payload, content, reasoning, finish

    lists_text = ""
    if two_step:
        _, lists_content, _, lists_finish = run_once(
            lists_path.read_text(encoding="utf-8"), f"{stem}_lists", lists_path
        )
        lists_text = extract_lists(lists_content) or ""
        if lists_finish != "stop" or not lists_text:
            raise SystemExit("closed lists not found; second request not sent")
        judge_template = judge_template.replace(LISTS_MARK, lists_text)
    judge_template = prepare_judge_prompt(judge_template)
    payload, content, _, _ = run_once(judge_template, stem, prompt_path)
    if two_step:
        counted, outside = part_a_rows(content, lists_text)
        count_path = raw_dir / f"{stem}_count.json"
        count_path.write_text(
            json.dumps({"counted": counted, "outside": outside}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps({"counted_n": len(counted), "outside_n": len(outside)}),
            flush=True,
        )


if __name__ == "__main__":
    main()
