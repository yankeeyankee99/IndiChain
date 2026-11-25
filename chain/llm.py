"""LLM prompts and report helpers. No Bohr shortlist, no invented tag names."""

from __future__ import annotations


def both_ends_prompt(sentence: str, left: str, right: str) -> str:
    return (
        f"Sentence:\n{sentence}\n\n"
        f"End A: {left}\n"
        f"End B: {right}\n\n"
        "Does this sentence write about both ends? Reply yes or no."
    )


def quote_in_text(quote: str, text: str) -> bool:
    q = (quote or "").strip().casefold()
    return bool(q) and q in (text or "").casefold()


def _extract_tag(line: str) -> tuple[str, str] | None:
    head, _, rest = (line or "").strip().partition(" ")
    token = head.upper().rstrip(":")
    if token not in {"DIRECT", "NARROWER", "NOT_MECHANISM", "NONE"}:
        return None
    return token, rest.strip().lstrip("|").strip()


def _strip_paper_id(quote: str) -> str:
    text = (quote or "").strip()
    if not text.startswith("[") or "]" not in text:
        return text
    bracket, _, rest = text.partition("]")
    if bracket[1:].isdigit():
        return rest.strip()
    return text


def names_in_quote(left: str, right: str, quote: str) -> bool:
    text = (quote or "").casefold()
    first = (left or "").strip().casefold()
    second = (right or "").strip().casefold()
    return bool(first and second and first in text and second in text)


def _names_line(line: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in (line or "").split("\t")]
    if len(parts) >= 3 and parts[0].upper() == "NAMES":
        return parts[1], parts[2]
    return None


def parse_extract(reply: str, blob: str) -> tuple[str, str]:
    """The tag may follow a preamble. A leading paper id is not part of the sentence.

    DIRECT and NARROWER name the two wordings used in the sentence. Both must appear there.
    """
    lines = [ln.strip() for ln in (reply or "").splitlines() if ln.strip()]
    labels = {"DIRECT": "direct", "NARROWER": "narrower", "NOT_MECHANISM": "not_a_mechanism"}
    for index, line in enumerate(lines):
        found = _extract_tag(line)
        if not found:
            continue
        tag, rest = found
        if tag == "NONE":
            return ("no_direct_sentence", "")
        if tag == "NOT_MECHANISM":
            quote = _strip_paper_id(rest or (lines[index + 1] if index + 1 < len(lines) else ""))
            if not quote or not quote_in_text(quote, blob):
                return ("quote_not_in_text", "")
            return ("not_a_mechanism", quote)
        names = _names_line(rest) if rest else None
        cursor = index + 1
        if names is None and cursor < len(lines):
            names = _names_line(lines[cursor])
            if names:
                cursor += 1
        quote = ""
        if cursor < len(lines) and not _names_line(lines[cursor]):
            quote = _strip_paper_id(lines[cursor])
        elif rest and not _names_line(rest):
            quote = _strip_paper_id(rest)
        if not names:
            return ("names_missing", "")
        if not quote or not quote_in_text(quote, blob):
            return ("quote_not_in_text", "")
        if not names_in_quote(names[0], names[1], quote):
            return ("names_missing", "")
        return (labels[tag], quote)
    return ("extractor_unreadable", "") if lines else ("extractor_unreadable", "")



def claim_prompt(abstract: str) -> str:
    return (
        f"Abstract:\n{abstract}\n\n"
        "List pathogenesis claims that appear in this abstract.\n"
        "Each line: CLAIM | QUOTE\n"
        "QUOTE must be copied from the abstract. Drop claims you cannot quote."
    )


def parse_claim_lines(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        claim, quote = line.split("|", 1)
        claim, quote = claim.strip(), quote.strip().strip('"')
        if claim and quote:
            rows.append({"claim": claim, "quote": quote})
    return rows


def keep_quoted_claims(rows: list[dict], text: str) -> list[dict[str, str]]:
    out = []
    for row in rows:
        claim = str(row.get("claim") or "").strip()
        quote = str(row.get("quote") or "").strip()
        if claim and quote_in_text(quote, text):
            out.append({"claim": claim, "quote": quote})
    return out


def write_report(*, target_name: str, target_id: str, hypotheses: list[dict]) -> str:
    lines = [
        "## Query",
        f"{target_name} ({target_id})",
        "",
        "## Notes",
        "Diseases are listed without ranking.",
        "",
        "## Hypotheses",
    ]
    for hypo in hypotheses:
        lines.append(f"### {hypo['name']} ({hypo.get('id') or ''})".rstrip())
        lines.append("## Path")
        for step, name, status in hypo["path"]:
            lines.append(f"- {step}: {name} — {status}")
        lines.append("## Claim")
        for claim in hypo["claims"]:
            if claim.get("status") == "direct evidence":
                lines.append(
                    f"- {claim.get('step')}: {claim.get('source')} {claim.get('quote')}"
                )
            else:
                lines.append(
                    f"- {claim.get('step')}: no source sentence {claim.get('pair') or ''}".rstrip()
                )
        lines.append("## Close")
        for pair in hypo.get("close") or []:
            lines.append(f"- {pair}")
        lines.append("")
    return "\n".join(lines) + "\n"


# One named chain. A reason has one move. Already is how many times that reason was handled.
_MOVES = {
    "no_direct_sentence": ("repropose", "repropose"),
    "no_hits": ("rephrase", "repropose"),
    "source_unavailable": ("retry", "stop"),
    "abstract_missing": ("retry", "stop"),
    "query_rejected": ("rephrase", "repropose"),
    "narrower": ("rename", "rename"),
    "direct": ("keep", "keep"),
    "model_unavailable": ("stop", "stop"),
    "model_timeout": ("retry", "stop"),
    "not_a_mechanism": ("retry", "repropose"),
    "quote_not_in_text": ("retry", "repropose"),
    "extractor_unreadable": ("retry", "repropose"),
    "node_unsearchable": ("reformat", "repropose"),
    "node_same_as_left": ("reformat", "repropose"),
    "node_is_target_end": ("join", "join"),
    "ends_mismatch": ("reformat", "stop"),
    "scope_unreadable": ("retry", "repropose"),
    "join_unreadable": ("retry", "search"),
    "attempt_limit": ("stop", "stop"),
    "sentence_limit": ("stop", "stop"),
}
_SENTENCE_CAP = 5
_ABANDON_CAP = 3
# Read when a node is proposed or a library sentence is kept. Not repeated on search or extract.
NODE_RULE = (
    "A node is one specific cell, process, or molecule.\n"
    "A disease is not a node. The only disease on the axis is the final end.\n"
    "Reject a name that is only a broad process word. Examples: activation, deposition, proliferation.\n"
    "A name that states which cell or molecule is doing it is kept. Example: T cell activation."
)


def move_for(reason: str, already: int) -> str:
    pair = _MOVES.get(reason, ("stop", "stop"))
    return pair[0 if already <= 0 else 1]


def disease_question(name: str, disease: str) -> str:
    return (
        "TASK disease\n"
        f"Name: {name}\nFinal disease: {disease}\n"
        "Only the final end of the axis may be a disease. "
        "An intermediate node is a cell, a process, or a molecule.\n"
        "If the name is not a disease, output DISEASE, tab, no.\n"
        "If the name is the final disease, including another name of that same disease, output DISEASE, tab, end.\n"
        "If the name is some other disease, output DISEASE, tab, other."
    )


def parse_disease_role(reply: str) -> str:
    value = _line_value(reply, "DISEASE").casefold()
    if value in ("no", "end", "other"):
        return value
    return ""


def name_problem(name: str, left: str, end: str) -> str:
    text = " ".join((name or "").split())
    if not text or any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return "node_unsearchable"
    if text.casefold() == " ".join((end or "").split()).casefold():
        return "node_is_target_end"
    if text.casefold() == " ".join((left or "").split()).casefold():
        return "node_same_as_left"
    return ""


def http_detail(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    reason = str(getattr(exc, "reason", "") or "")
    body = ""
    read = getattr(exc, "read", None)
    if read is not None:
        try:
            raw = read()
        except Exception:
            raw = b""
        body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    text = " ".join(part for part in (str(code or ""), reason, body.strip()) if part).strip()
    return text[:400]


def http_reason(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code == 402 or "ARK_API_KEY" in str(exc):
        return "model_unavailable"
    if "BOHRIUM_API_KEY" in str(exc):
        return "source_unavailable"
    if code == 429 or (isinstance(code, int) and code >= 500):
        return "source_unavailable"
    if isinstance(code, int) and code >= 400:
        return "query_rejected"
    return "source_unavailable"


def _model_reason(exc: BaseException) -> str:
    if getattr(exc, "code", None) == 402 or "ARK_API_KEY" in str(exc):
        return "model_unavailable"
    return "model_timeout"


def _bodies_missing(papers: list[dict]) -> bool:
    for paper in papers:
        text = str(paper.get("text") or "").strip()
        title = str(paper.get("title") or "").strip()
        if text and text != title:
            return False
    return True


def _paper_with(quote: str, papers: list[dict]) -> dict:
    for paper in papers:
        if quote_in_text(quote, str(paper.get("text") or "")):
            return paper
    return {}


def _reason_before(text: str, labels: set[str]) -> str:
    """Judgment text that appears before the choice lines. Empty when the choice comes first."""
    lines = (text or "").splitlines()
    reason_at = None
    choice_at = None
    for index, line in enumerate(lines):
        parts = [part.strip() for part in line.split("\t")]
        if not parts:
            continue
        head = parts[0]
        if head in labels and len(parts) >= 4:
            choice_at = index
            break
        if head.upper() == "REASON" or head.upper().startswith("REASON:"):
            reason_at = index
    if reason_at is None or (choice_at is not None and choice_at < reason_at):
        return ""
    chunks = []
    first = [part.strip() for part in lines[reason_at].split("\t")]
    head = first[0]
    if head.upper() == "REASON" and len(first) > 1:
        chunks.append("\t".join(first[1:]))
    elif head.upper().startswith("REASON:"):
        chunks.append(head.split(":", 1)[1].strip())
        if len(first) > 1:
            chunks.append("\t".join(first[1:]))
    stop = choice_at if choice_at is not None else len(lines)
    chunks.extend(lines[reason_at + 1 : stop])
    return "\n".join(chunks).strip()


def _parse_ends(text: str) -> dict[str, list[str]] | None:
    found: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) >= 4 and parts[0] in ("B", "E"):
            found[parts[0]] = parts
    if "B" not in found or "E" not in found:
        return None
    return found


def _cited_row(records: list[dict], disease_rows: list[dict], cite: str) -> tuple[dict | None, str]:
    token = cite.strip().upper().replace("[", "").replace("]", "")
    if len(token) < 2 or token[0] not in "TD" or not token[1:].isdigit():
        return None, ""
    rows = records if token[0] == "T" else disease_rows
    for row in rows:
        if int(row.get("n") or 0) != int(token[1:]):
            continue
        if token[0] == "T":
            return row, "\n".join(str(line) for line in (row.get("lines") or []))
        return row, str(row.get("quote") or "")
    return None, ""


def _quotes(steps: list[dict]) -> list[str]:
    return [str(q).strip() for step in steps for q in (step.get("quotes") or []) if str(q).strip()]


def _can_add(steps: list[dict], quote: str, cap: int, reserve: bool, held: tuple[str, ...] = ()) -> bool:
    have = {q for q in _quotes(steps)}
    have.update(item.strip() for item in held if item.strip())
    if quote.strip() in have:
        return True
    limit = cap - (1 if reserve else 0)
    return len(have) < limit


def build_named_chain(
    *,
    target: str,
    disease: str,
    records: list[dict],
    disease_rows: list[dict],
    direction: str,
    ask,
    fetch,
    cap: int = _SENTENCE_CAP,
) -> dict:
    """Propose one intermediate at a time. Each failure keeps its own reason.

    ponytail: a reworded node is not matched to an abandoned name; three abandons stop the loop.
    Upgrade: reject a node whose search terms are the abandoned pair.
    ponytail: NONE inside the retrieved batch is no_direct_sentence, not proof the link is absent.
    ponytail: DIRECT, NARROWER, and NOT_MECHANISM are the extractor's labels; only the quote is checked.
    """
    events: list[dict] = []
    audit: list[dict] = []
    ends_reason = ""
    disease_end = None

    def note(reason: str, move: str, node: str = "") -> None:
        events.append({"reason": reason, "move": move, "node": node})

    def call(body: str) -> tuple[str, str]:
        from run import last_reasoning

        head, _, rest = body.partition("\n")
        # An empty direction is not glued on. Task text already carries its own rule.
        extra = (direction or "").strip()
        prompt = head + ("\n" + extra if extra else "") + "\n" + rest
        try:
            content = str(ask(prompt) or "")
        except Exception as exc:  # noqa: BLE001 — one model failure, classified below
            err = _model_reason(exc)
            if getattr(exc, "code", None) == 429:
                print("HTTP Error 429", flush=True)
            audit.append({"task": head, "prompt": prompt, "reasoning": "", "reply": "", "error": err})
            return "", err
        audit.append({"task": head, "prompt": prompt, "reasoning": last_reasoning(), "reply": content})
        return content, ""

    def finish(steps: list[dict], stop: str, reached: bool = False, paths: list | None = None) -> dict:
        if stop:
            note(stop, "stop")
        end = None
        if disease_end:
            end = dict(disease_end)
            end["linked"] = reached
        return {
            "steps": steps,
            "paths": paths or ([steps] if steps else []),
            "stop": stop,
            "events": events,
            "reached": reached,
            "disease_end": end,
            "audit": audit,
            "ends_reason": ends_reason,
        }

    ends_body = (
        "TASK ends\n"
        f"Target A: {target}\nDisease F: {disease}\n"
        "The two libraries below are the only information for this choice.\n"
        "Each row is a library number and one sentence from the abstract.\n"
        "Write the judgment first, then the choice. "
        "The judgment names one target-library row and one disease-library row and states what in those two sentences makes them the ends.\n"
        "Output a REASON line, the judgment under it, then two choice lines.\n"
        "B, tab, T number, tab, the English name of the entity in that sentence, tab, the sentence copied from that row.\n"
        "E, tab, D number, tab, the English name of the entity in that sentence, tab, the sentence copied from that row.\n"
        "Another name of the same entity is allowed.\n"
        "A choice line before the REASON line is rejected.\n\n"
        "Target library:\n" + _lib_lines(records, "T") + "\n\nDisease library:\n" + _lib_lines(disease_rows, "D")
    )
    reply, err = call(ends_body)
    if err == "model_timeout":
        reply, err = call(ends_body)
    if err:
        return finish([], err)
    chosen = _accepted_ends(reply, records, disease_rows)
    reason = _reason_before(reply, {"B", "E"})
    if chosen and name_problem(chosen[0], target, "") == "node_same_as_left":
        chosen = None
    if not chosen or not reason:
        note("ends_mismatch", "reformat")
        reply, err = call(
            "TASK reformat\nWrite the REASON line and the judgment first. Then the B line and the E line. "
            "A choice line before REASON is rejected.\n\n"
            + ends_body.split("\n", 1)[1]
        )
        if err:
            return finish([], err)
        chosen = _accepted_ends(reply, records, disease_rows)
        reason = _reason_before(reply, {"B", "E"})
        if chosen and name_problem(chosen[0], target, "") == "node_same_as_left":
            chosen = None
    if not chosen or not reason:
        return finish([], "ends_unreadable")
    ends_reason = reason
    b_item, b_quote, b_row, e_item, e_quote, e_row = chosen
    print(f"ends B {b_item[:60]} | E {e_item[:60]}", flush=True)
    disease_end = _step(e_item, disease, e_quote, "D", e_row)
    held = (e_quote,)
    steps = [_step(target, b_item, b_quote, "T", b_row)]
    def _joined(outcome: dict, branch: list[dict], add_disease_end: bool = False) -> dict:
        if outcome["kind"] == "stop":
            return outcome
        if outcome["kind"] in ("reached", "reached_disease"):
            if add_disease_end and outcome["kind"] == "reached" and name_problem(disease, e_item, "") != "node_same_as_left":
                branch.append(dict(disease_end))
            print("joined disease" if outcome["kind"] == "reached_disease" else "joined E", flush=True)
            return {"kind": "reached", "steps": branch}
        if outcome["kind"] == "extended":
            return {"kind": "extend", "left": outcome["left"], "steps": branch}
        return {"kind": "fail", "reason": outcome.get("reason") or "no_direct_sentence"}

    def _link_one(node: str, left: str, steps: list[dict], join_tried: bool) -> dict:
        branch = list(steps)
        if name_problem(node, left, e_item) != "node_is_target_end" and name_problem(node, left, disease) != "node_is_target_end":
            role, err = _disease_role(call, node, disease)
            if err:
                return {"kind": "stop", "reason": err}
            if role == "other":
                return {"kind": "fail", "reason": "node_is_disease"}
            if role == "end":
                return _joined(_join(call, fetch, branch, left, disease, cap, note, held, disease), branch)
        if name_problem(node, left, e_item) == "node_is_target_end":
            if join_tried:
                return {"kind": "fail", "reason": "node_is_target_end"}
            return _joined(_join(call, fetch, branch, left, e_item, cap, note, held, disease), branch, True)
        if name_problem(node, left, disease) == "node_is_target_end":
            return _joined(_join(call, fetch, branch, left, disease, cap, note, held, disease), branch)
        found = search_link(left, node, fetch, call, note)
        if found["kind"] == "stop":
            return found
        if found["kind"] == "repropose":
            return {"kind": "fail", "reason": found["reason"]}
        right, quote, paper, err = _accept_hit(call, found, left, e_item, note, disease)
        if err == "node_is_disease":
            return {"kind": "fail", "reason": err}
        if err:
            return {"kind": "stop", "reason": err}
        if not right:
            return {"kind": "fail", "reason": "scope_unreadable"}
        if not _place(branch, left, right, quote, paper, cap, True, held):
            return {"kind": "stop", "reason": "sentence_limit"}
        if name_problem(right, "", disease) == "node_is_target_end":
            print("joined disease", flush=True)
            return {"kind": "reached", "steps": branch}
        joined = _joined(_join(call, fetch, branch, right, e_item, cap, note, held, disease), branch, True)
        if joined["kind"] == "fail":
            return {"kind": "extend", "left": right, "steps": branch}
        return joined

    left = b_item
    work = [(left, steps, [], False)]
    reached_paths: list[list[dict]] = []
    partial = steps
    stop_reason = ""
    while work:
        left, steps, abandoned, join_tried = work.pop()
        if len(steps) >= len(partial):
            partial = steps
        if len(abandoned) >= _ABANDON_CAP:
            stop_reason = stop_reason or "attempt_limit"
            continue
        if not _can_add(steps, "pending", cap, True, held):
            stop_reason = "sentence_limit"
            continue
        nodes, problem, err = _propose_nodes(
            call, target, disease, b_item, b_quote, e_item, e_quote, left, steps, abandoned
        )
        if err:
            return finish(steps, err, paths=reached_paths)
        if problem or not nodes:
            print(f"node rejected {problem}", flush=True)
            note(problem or "node_unsearchable", "repropose", nodes[0] if nodes else "")
            abandoned.append({"node": nodes[0] if nodes else "", "reason": problem or "node_unsearchable"})
            work.append((left, steps, abandoned, join_tried))
            continue
        extended = []
        failed = []
        reached_here = False
        for node in nodes:
            print(f"propose {node[:80]}", flush=True)
            result = _link_one(node, left, steps, join_tried)
            if result["kind"] == "stop":
                return finish(result.get("steps") or steps, result["reason"], paths=reached_paths)
            if result["kind"] == "fail":
                print(f"repropose {result['reason']} {node[:60]}", flush=True)
                note(result["reason"], "repropose", node)
                failed.append({"node": node, "reason": result["reason"]})
                continue
            if result["kind"] == "reached":
                reached_paths.append(result["steps"])
                reached_here = True
                continue
            extended.append((result["left"], result["steps"], [], False))
        if extended:
            work.extend(reversed(extended))
            continue
        if failed and not reached_here:
            abandoned = abandoned + failed
            if len(abandoned) < _ABANDON_CAP:
                work.append((left, steps, abandoned, True))
                continue
            stop_reason = stop_reason or "attempt_limit"
    chosen_steps = reached_paths[0] if reached_paths else partial
    return finish(chosen_steps, "" if reached_paths else stop_reason, bool(reached_paths), reached_paths)


def _lib_lines(rows: list[dict], kind: str) -> str:
    """Rows the model judges. A stored title is not an abstract sentence."""
    lines = []
    for row in rows:
        text = " ".join(str(line) for line in (row.get("lines") or [])) if kind == "T" else str(row.get("quote") or "")
        title = str(row.get("title") or "").strip()
        if not text.strip() or text.strip().casefold() == title.casefold():
            continue
        lines.append(f"{kind}{row.get('n')}\t{text}")
    return "\n".join(lines)


def _step(left: str, right: str, quote: str, side: str, row: dict) -> dict:
    return {
        "status": "confirmed",
        "step": f"{left} → {right}",
        "quotes": [quote],
        "side": side,
        "n": row.get("n"),
        "paper_id": str(row.get("paper_id") or ""),
        "title": str(row.get("title") or ""),
    }


def _accepted_ends(reply: str, records: list[dict], disease_rows: list[dict]):
    found = _parse_ends(reply)
    if not found:
        return None
    out = []
    for key in ("B", "E"):
        cite, item, quote = found[key][1], found[key][2], "\t".join(found[key][3:])
        row, text = _cited_row(records, disease_rows, cite)
        if not row or not item or not quote_in_text(quote, text):
            return None
        if name_problem(item, "", "") == "node_unsearchable":
            return None
        out.extend((item, quote, row))
    return tuple(out)


def _propose_nodes(call, target, disease, b_item, b_quote, e_item, e_quote, left, steps, abandoned):
    body = (
        "TASK node\n"
        f"A: {target}\nB: {b_item}\nB sentence: {b_quote}\n"
        f"E: {e_item}\nE sentence: {e_quote}\nF: {disease}\n"
        f"Current left: {left}\n"
        "Chain:\n" + "\n".join(step["step"] for step in steps) + "\n"
        "Abandoned from this left:\n"
        + "\n".join(f"{item['node']}: {item['reason']}" for item in abandoned)
        + "\nPropose the next English nodes from the current left toward E.\n"
        "Name only nodes you consider credible and directed toward F.\n"
        + NODE_RULE + "\n"
        + "Do not rename an abandoned node.\n"
        "Output at most three lines. Fewer is correct. Do not add names to reach three.\n"
        "Each line is NODE, tab, the name."
    )
    reply, err = _call_retry(call, body)
    if err:
        return [], "", err
    good = []
    for node in node_lines(reply):
        problem = name_problem(node, left, e_item)
        if problem in ("", "node_is_target_end"):
            good.append(node)
    if good:
        return good, "", ""
    raw = node_lines(reply)
    node = raw[0] if raw else ""
    problem = name_problem(node, left, e_item) if node else "node_unsearchable"
    reply, err = _call_retry(call, "TASK reformat\n" + body + f"\nThe name was rejected: {problem}. Output a different NODE line.")
    if err:
        return [], "", err
    node = _line_value(reply, "NODE")
    problem = name_problem(node, left, e_item)
    if problem in ("", "node_is_target_end"):
        return [node], "", ""
    return [], problem, ""


def _propose(call, target, disease, b_item, b_quote, e_item, e_quote, left, steps, abandoned):
    nodes, problem, err = _propose_nodes(
        call, target, disease, b_item, b_quote, e_item, e_quote, left, steps, abandoned
    )
    return (nodes[0] if nodes else ""), problem, err


def _call_retry(call, body: str) -> tuple[str, str]:
    reply, err = call(body)
    if err == "model_timeout":
        reply, err = call(body)
    return reply, err


def node_lines(reply: str, limit: int = 3) -> list[str]:
    """At most three NODE names, in the order written. Fewer is kept as written."""
    found: list[str] = []
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if not parts or parts[0].upper() != "NODE" or len(parts) < 2 or not parts[1]:
            continue
        if parts[1] not in found:
            found.append(parts[1])
        if len(found) == limit:
            break
    return found


def _line_value(reply: str, label: str) -> str:
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if parts and parts[0].upper() == label and len(parts) > 1:
            return parts[1]
    return ""


def _disease_role(call, name: str, disease: str) -> tuple[str, str]:
    if name_problem(name, "", disease) == "node_is_target_end":
        return "end", ""
    reply, err = _call_retry(call, disease_question(name, disease))
    if err:
        return "", err
    role = parse_disease_role(reply)
    if role:
        return role, ""
    reply, err = _call_retry(call, "TASK reformat\n" + disease_question(name, disease))
    if err:
        return "", err
    return parse_disease_role(reply) or "other", ""


def _guard_disease(call, item: str, disease: str) -> tuple[str, str]:
    """Return the name to keep, or an error. Another disease is not an intermediate node."""
    if not disease or name_problem(item, "", disease) == "node_is_target_end":
        return item, ""
    role, err = _disease_role(call, item, disease)
    if err:
        return "", err
    if role == "other":
        return "", "node_is_disease"
    if role == "end":
        return disease, ""
    return item, ""


def _accept_hit(call, found: dict, left: str, end: str, note, disease: str = "") -> tuple[str, str, dict, str]:
    if found["kind"] == "direct":
        if name_problem(found["right"], left, end) == "node_same_as_left":
            return "", "", {}, ""
        return found["right"], found["quote"], found["paper"], ""
    reply, err = _call_retry(
        call,
        "TASK rename\nThe sentence is narrower than the proposed node.\nSentence: "
        + found["quote"]
        + "\nName the narrower entity. Another name of the same entity is allowed.\n"
        "Do not name a disease. The final disease is not an intermediate node.\n"
        "Output only that English name.",
    )
    if err:
        return "", "", {}, err
    item = " ".join((reply or "").split())
    if name_problem(item, left, end) == "node_unsearchable":
        note("scope_unreadable", "retry", item)
        reply, err = _call_retry(
            call,
            "TASK rename\nName the narrower entity. Another name of the same entity is allowed.\n"
            "Do not name a disease.\n"
            "Output only that English name.\n" + found["quote"],
        )
        if err:
            return "", "", {}, err
        item = " ".join((reply or "").split())
    problem = name_problem(item, left, end)
    if not item or problem in ("node_unsearchable", "node_same_as_left"):
        return "", "", {}, ""
    item, err = _guard_disease(call, item, disease)
    if err:
        return "", "", {}, err
    return item, found["quote"], found["paper"], ""


def _place(steps, left, right, quote, paper, cap, reserve, held=()) -> bool:
    step = {
        "status": "confirmed",
        "step": f"{left} → {right}",
        "quotes": [quote],
        "paper_id": str(paper.get("paper_id") or ""),
        "title": str(paper.get("title") or ""),
    }
    if not _can_add(steps, quote, cap, reserve, held):
        return False
    steps.append(step)
    return True


def _join(call, fetch, steps, left, end, cap, note, held=(), disease: str = "") -> dict:
    reply, err = _call_retry(
        call,
        f"TASK join\nCurrent node: {left}\nE: {end}\n"
        "Can this node connect directly to E? Output JOIN, tab, yes or no.",
    )
    if err:
        return {"kind": "stop", "reason": err}
    if parse_join(reply) not in ("yes", "no"):
        note("join_unreadable", "retry", left)
        reply, err = _call_retry(
            call,
            f"TASK join\nCurrent node: {left}\nE: {end}\nOutput JOIN, tab, yes or no.",
        )
        if err:
            return {"kind": "stop", "reason": err}
        if parse_join(reply) not in ("yes", "no"):
            note("join_unreadable", "search", left)
    found = search_link(left, end, fetch, call, note)
    if found["kind"] == "stop":
        return found
    if found["kind"] == "repropose":
        note(found["reason"], "search", left)
        return {"kind": "miss", "reason": found["reason"]}
    right, quote, paper, err = _accept_hit(call, found, left, end, note, disease)
    if err == "node_is_disease":
        return {"kind": "miss", "reason": "node_is_disease"}
    if err:
        return {"kind": "stop", "reason": err}
    if not right:
        return {"kind": "miss", "reason": "scope_unreadable"}
    if not _place(steps, left, right, quote, paper, cap, False, held):
        return {"kind": "stop", "reason": "sentence_limit"}
    if disease and name_problem(right, "", disease) == "node_is_target_end" and name_problem(end, "", disease) != "node_is_target_end":
        return {"kind": "reached_disease", "reason": ""}
    if right.casefold() == end.casefold():
        return {"kind": "reached", "reason": ""}
    return {"kind": "extended", "reason": "", "left": right}


def parse_join(reply: str) -> str:
    value = _line_value(reply, "JOIN").casefold()
    if value in ("yes", "no"):
        return value
    return ""


def link_sources() -> tuple[str, ...]:
    return ("pubmed",)


def query_question(left: str, right: str, rejected: str = "") -> str:
    text = (
        "TASK query\n"
        f"Earlier item: {left}\nLater item: {right}\n"
        "Write one complete PubMed search with field tags and Boolean operators.\n"
        "Output QUERY, tab, the search.\n"
    )
    if rejected:
        text += (
            f"PubMed rejected the previous search: {rejected}\n"
            "Say what in that search is invalid, then output a corrected QUERY line.\n"
        )
    return text


def parse_query(reply: str) -> str:
    return _line_value(reply, "QUERY").strip()


def search_link(left: str, right: str, fetch, call, note, *, rephrased: bool = False, label: str = "") -> dict:
    shown = label or right
    found_narrow = None
    reasons = []
    for source in link_sources():
        outcome = _read_source(left, right, source, fetch, call)
        if outcome["kind"] == "direct":
            outcome["right"] = shown
            return outcome
        if outcome["kind"] == "narrower" and found_narrow is None:
            found_narrow = outcome
        if outcome["kind"] == "stop" and outcome["reason"] in ("model_unavailable", "model_timeout"):
            return outcome
        reasons.append(outcome)
    if found_narrow:
        found_narrow["right"] = shown
        return found_narrow
    labels = [item["reason"] for item in reasons]
    if any(label == "no_direct_sentence" for label in labels):
        return {"kind": "repropose", "reason": "no_direct_sentence", "quote": "", "paper": {}, "right": right}
    if any(label == "not_a_mechanism" for label in labels):
        return {"kind": "repropose", "reason": "not_a_mechanism", "quote": "", "paper": {}, "right": right}
    if any(label == "quote_not_in_text" for label in labels):
        return {"kind": "repropose", "reason": "quote_not_in_text", "quote": "", "paper": {}, "right": right}
    if all(label == "no_hits" for label in labels):
        return {"kind": "repropose", "reason": "no_hits", "quote": "", "paper": {}, "right": right}
    if all(label == "abstract_missing" for label in labels):
        return {"kind": "stop", "reason": "abstract_missing", "quote": "", "paper": {}, "right": right}
    if "query_rejected" in labels:
        return {"kind": "repropose", "reason": "query_rejected", "quote": "", "paper": {}, "right": right}
    return {"kind": "stop", "reason": "source_unavailable", "quote": "", "paper": {}, "right": right}


def _fetch_query(fetch, query: str, source: str) -> tuple[list, str, str]:
    try:
        return fetch(query, source) or [], "", ""
    except Exception as exc:  # noqa: BLE001 — classified, not mixed with an empty hit list
        return [], http_reason(exc), http_detail(exc)


def _read_source(left: str, right: str, source: str, fetch, call) -> dict:
    # ponytail: three rejected searches stop this node; a later syntax error on the same pair is not retried.
    rejected = ""
    for _attempt in range(3):
        reply, err = _call_retry(call, query_question(left, right, rejected))
        if err:
            kind = "stop" if err in ("model_unavailable", "model_timeout") else "repropose"
            return {"kind": kind, "reason": err, "quote": "", "paper": {}}
        query = parse_query(reply)
        if not query:
            rejected = "The reply had no QUERY line."
            continue
        papers, reason, detail = _fetch_query(fetch, query, source)
        if reason == "source_unavailable":
            papers, reason, detail = _fetch_query(fetch, query, source)
        if reason == "query_rejected":
            rejected = detail or "PubMed rejected the search."
            continue
        if reason:
            kind = "stop" if reason in ("source_unavailable", "model_unavailable", "model_timeout") else "repropose"
            return {"kind": kind, "reason": reason, "quote": "", "paper": {}}
        if not papers:
            return {"kind": "no_hits", "reason": "no_hits", "quote": "", "paper": {}}
        if _bodies_missing(papers):
            papers, reason, detail = _fetch_query(fetch, query, source)
            if reason or not papers or _bodies_missing(papers):
                return {"kind": "stop", "reason": "abstract_missing", "quote": "", "paper": {}}
        return _extract_papers(papers, left, right, call)
    return {"kind": "repropose", "reason": "query_rejected", "quote": "", "paper": {}}


def _extract_papers(papers: list[dict], left: str, right: str, call) -> dict:
    seen = "no_direct_sentence"
    for start in range(0, len(papers), 10):
        chunk = papers[start : start + 10]
        blob = "\n\n".join(f"[{paper.get('paper_id') or ''}]\n{paper.get('text') or ''}" for paper in chunk)
        for extract_try in (0, 1):
            reply, err = call(
                "TASK extract\n"
                f"Earlier item: {left}\nLater item: {right}\n"
                "If one sentence mentions both items, the first line is DIRECT.\n"
                "Another name or abbreviation counts when it is the same thing.\n"
                "The next line is NAMES, tab, the wording used for the earlier item, tab, the wording used for the later item.\n"
                "The line after that is the sentence copied from the abstract.\n"
                "If the sentence states a narrower item, the first line is NARROWER, then the same NAMES line and the sentence.\n"
                "Otherwise the first line is NONE.\n\n" + blob
            )
            if err == "model_timeout" and extract_try == 0:
                continue
            if err:
                return {"kind": "stop", "reason": err, "quote": "", "paper": {}}
            tag, quote = parse_extract(reply, blob)
            if tag in ("quote_not_in_text", "extractor_unreadable", "not_a_mechanism", "names_missing") and extract_try == 0:
                continue
            if tag in ("direct", "narrower"):
                return {"kind": tag, "reason": tag, "quote": quote, "paper": _paper_with(quote, chunk)}
            seen = tag if tag in ("not_a_mechanism", "quote_not_in_text", "names_missing") else seen
            break
    kind = "repropose"
    return {"kind": kind, "reason": seen, "quote": "", "paper": {}}
