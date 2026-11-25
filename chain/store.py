"""Run records with as_of / fetched_at / source_release."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import sqlite3


def open_store(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY,
            as_of TEXT,
            fetched_at TEXT,
            source_release TEXT,
            kind TEXT,
            payload TEXT
        )"""
    )
    return conn


def add_record(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict,
    as_of: date | None = None,
    source_release: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO records(as_of, fetched_at, source_release, kind, payload) VALUES (?,?,?,?,?)",
        (
            as_of.isoformat() if as_of else None,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source_release,
            kind,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()


def latest_papers_payload(
    conn: sqlite3.Connection,
    *,
    query: str,
    as_of: date | None = None,
    harvest_and: list[str] | None = None,
) -> dict | None:
    as_of_text = as_of.isoformat() if as_of else None
    harvest = list(harvest_and or [])
    fallback = None
    for as_of_row, raw in conn.execute(
        "SELECT as_of, payload FROM records WHERE kind = 'papers' ORDER BY id DESC"
    ):
        payload = json.loads(raw)
        if as_of_text is not None and as_of_row != as_of_text:
            continue
        if payload.get("query") == query:
            return payload
        if fallback is None and payload.get("harvest_and") == harvest:
            fallback = payload
    return fallback


def latest_e_reviews_payload(
    conn: sqlite3.Connection,
    *,
    mesh_id: str,
    as_of: date | None = None,
) -> dict | None:
    as_of_text = as_of.isoformat() if as_of else None
    for as_of_row, raw in conn.execute(
        "SELECT as_of, payload FROM records WHERE kind = 'e_reviews' ORDER BY id DESC"
    ):
        payload = json.loads(raw)
        if payload.get("mesh_id") != mesh_id:
            continue
        if as_of_text is not None and as_of_row != as_of_text:
            continue
        return payload
    return None
