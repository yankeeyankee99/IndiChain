"""Human chain report. Libraries stay in their own files."""

from __future__ import annotations

import html


def render_case(case: dict) -> tuple[str, str, dict]:
    steps = list(case.get("chain") or [])
    payload = {
        "query": {
            "target": case.get("target") or "",
            "target_id": case.get("target_id") or "",
            "disease": case.get("disease") or "",
            "disease_mesh": case.get("disease_mesh") or "",
            "sources": list(case.get("sources") or []),
            "as_of": case.get("as_of") or "",
        },
        "path": str(case.get("path") or "").strip(),
        "stop": str(case.get("stop") or "").strip(),
        "ends_reason": str(case.get("ends_reason") or "").strip(),
        "chain": steps,
        "disease_end": case.get("disease_end"),
    }
    return _markdown(payload), _page(payload), payload


def _markdown(payload: dict) -> str:
    query = payload["query"]
    steps = payload["chain"]
    lines = [
        "# Indicator3",
        "",
        "## Query",
        "- **Target**: "
        + str(query["target"])
        + (f" (`{query['target_id']}`)" if query["target_id"] else ""),
        f"- **Disease**: {query['disease']} (`{query['disease_mesh']}`)",
        f"- **Sources**: {', '.join(query['sources']) or 'none'}",
        f"- **As of**: {query['as_of'] or 'none'}",
        "",
    ]
    if payload.get("ends_reason"):
        lines.extend([
            "## Ends judgment",
            payload["ends_reason"],
            "",
            "The rows available for this judgment are the target library and the disease library sent in that call. The call is stored in chain.json under audit, task TASK ends.",
            "",
        ])
    lines.extend([
        "## Chain",
    ])
    if payload.get("stop"):
        lines.append(f"- **Stop**: {payload['stop']}")
    if payload["path"]:
        lines.append(f"- **Reason**: {payload['path']}")
    if not steps:
        lines.append("- none")
    else:
        lines.append("- **Path**:")
        for index, step in enumerate(steps, start=1):
            lines.append(
                f"  {index}. {step.get('left') or ''} → {step.get('right') or ''} (`{step.get('status') or ''}`)"
            )
        for step in steps:
            left = step.get("left") or ""
            right = step.get("right") or ""
            lines.append(f"- **Claim**: {left} → {right} (`{step.get('status') or ''}`)")
            quote = str(step.get("quote") or "").strip()
            if quote:
                lines.append("  - **Evidence**:")
                library = str(step.get("library") or "").strip()
                if library:
                    lines.append(f"    - **Library**: {library}")
                lines.append(f"    - **Source**: {_source(step)}")
                lines.append(f'      - **Verified Quote**: "{quote}"')
            else:
                lines.append(f"  - **Gap**: Missing literature pin for {left} → {right}")
    end = payload.get("disease_end")
    if end and not end.get("linked"):
        lines.extend(["", "## Disease end", "- **Not linked**"])
        lines.append(f"- {end.get('left') or ''} → {end.get('right') or ''}")
        library = str(end.get("library") or "").strip()
        if library:
            lines.append(f"  - **Library**: {library}")
        lines.append(f"  - **Source**: {_source(end)}")
        quote = str(end.get("quote") or "").strip()
        if quote:
            lines.append(f'  - **Verified Quote**: "{quote}"')
    return "\n".join(lines) + "\n"


def _page(payload: dict) -> str:
    query = payload["query"]
    target = html.escape(str(query["target"] or "Indicator3"))
    disease = html.escape(str(query["disease"] or ""))
    mesh = html.escape(str(query["disease_mesh"] or ""))
    sources = html.escape(", ".join(query["sources"]) or "none")
    as_of = html.escape(str(query["as_of"] or "none"))
    reason = html.escape(payload["path"])
    reason_html = f'<p class="reason">{reason}</p>' if reason else ""
    stop = html.escape(payload.get("stop") or "")
    stop_html = f'<p class="reason">Stop: {stop}</p>' if stop else ""
    steps = payload["chain"]
    if not steps:
        body = stop_html + "<p>none</p>"
    else:
        path_items = "".join(
            f"<li>{html.escape(str(step.get('left') or ''))} → "
            f"{html.escape(str(step.get('right') or ''))}</li>"
            for step in steps
        )
        hops = "".join(_hop(step) for step in steps)
        body = f'<ol class="path">{path_items}</ol>{stop_html}{reason_html}<ol class="timeline">{hops}</ol>'
    end = payload.get("disease_end")
    end_html = ""
    if end and not end.get("linked"):
        end_html = "<h2>Disease end</h2><p>Not linked</p><ol class=\"timeline\">" + _hop(end) + "</ol>"
    judgment = html.escape(payload.get("ends_reason") or "")
    judgment_html = ""
    if judgment:
        judgment_html = (
            "<h2>Ends judgment</h2><p>"
            + judgment.replace("\n", "<br/>")
            + "</p><p>The rows available for this judgment are stored with the TASK ends call in chain.json.</p>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>{target} — {disease}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 880px; margin: 0 auto; padding: 28px 24px 64px; color: #1b1b1b; line-height: 1.5; }}
h1, h2 {{ color: #1a5276; }}
.subtitle {{ color: #555; }}
.pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.8em; font-weight: 600; color: #fff; background: #7f8c8d; }}
.pill.supported {{ background: #1e8449; }}
.pill.inferred {{ background: #b7950b; }}
ol.path {{ padding-left: 22px; }}
p.reason {{ padding: 12px 14px; background: #eaf2f8; border-left: 4px solid #1a5276; }}
ol.timeline {{ list-style: none; padding: 0; }}
li.hop {{ padding: 16px 16px 16px 28px; margin: 0 0 18px; background: #f8f9fa; border-left: 4px solid #3498db; }}
.source-card {{ background: #fff; border: 1px solid #dde2e5; border-radius: 6px; padding: 12px 14px; margin-top: 12px; }}
.source-meta {{ font-size: 0.9em; margin-bottom: 8px; }}
.source-meta a {{ color: #1a5276; }}
p.gap {{ color: #7f8c8d; }}
</style>
</head>
<body>
<h1>{target}</h1>
<p class="subtitle">{disease} · {mesh} · {sources} · as of {as_of}</p>
{judgment_html}
<h2>Chain</h2>
{body}
{end_html}
</body>
</html>
"""


def _hop(step: dict) -> str:
    status = str(step.get("status") or "")
    kind = "supported" if status in {"确证", "confirmed"} else "inferred" if status in {"推断", "inferred"} else "hypothesized"
    status = {"确证": "confirmed", "推断": "inferred", "假设": "hypothesized"}.get(status, status)
    left = html.escape(str(step.get("left") or ""))
    right = html.escape(str(step.get("right") or ""))
    quote = str(step.get("quote") or "").strip()
    head = (
        f'<li class="hop"><span class="pill {kind}">{html.escape(status)}</span> '
        f"<b>{left} → {right}</b>"
    )
    if not quote:
        return head + f'<p class="gap">Missing literature pin for {left} → {right}</p></li>'
    library = str(step.get("library") or "").strip()
    library_html = f"{html.escape(library)} · " if library else ""
    return (
        head
        + '<div class="source-card"><div class="source-meta">'
        + library_html
        + _source_html(step)
        + "</div><p>"
        + html.escape(quote)
        + "</p></div></li>"
    )


def _source_html(step: dict) -> str:
    title = html.escape(str(step.get("title") or "").strip())
    paper_id = str(step.get("paper_id") or "").strip()
    label = html.escape(paper_id)
    if paper_id.isdigit() and len(paper_id) <= 8:
        ident = f'<a href="https://pubmed.ncbi.nlm.nih.gov/{label}/">{label}</a>'
    elif paper_id.lower().startswith("10."):
        ident = f'<a href="https://doi.org/{label}">{label}</a>'
    else:
        ident = label
    if title and paper_id:
        return f"<strong>{title}</strong> [{ident}]"
    return f"<strong>{title or ident or 'Unknown'}</strong>"


def _source(row: dict) -> str:
    title = str(row.get("title") or "").strip()
    paper_id = str(row.get("paper_id") or "").strip()
    if title and paper_id:
        return f"{title} [{paper_id}]"
    return title or paper_id or "Unknown"


if __name__ == "__main__":
    linked = _source_html({"title": "A paper", "paper_id": "10.1038/s41586"})
    assert "https://doi.org/10.1038/s41586" in linked
    number = _source_html({"title": "A paper", "paper_id": "817350575447343109"})
    assert "817350575447343109" in number and "doi.org" not in number
    print("ok")
