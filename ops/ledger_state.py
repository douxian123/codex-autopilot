#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Converge append-only ledger rows by event time, not physical line order.

Truth synchronization can append an older snapshot after a newer terminal row.
Taking the last physical line then revives a completed item. Timestamped rows
therefore win by timestamp; rows without a parseable timestamp retain the old
last-line behavior.
"""

from datetime import datetime, timezone


def _timestamp(value):
    """Return an aware UTC datetime for valid ISO timestamps, otherwise None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest_ledger_by_id(rows):
    """Return each ledger id's latest row without letting an old snapshot win."""
    grouped = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        record_id = row.get("id")
        if record_id:
            grouped.setdefault(record_id, []).append((index, row))

    latest = {}
    for record_id, events in grouped.items():
        timestamped = [
            (_timestamp(row.get("ts")), index, row)
            for index, row in events
            if _timestamp(row.get("ts")) is not None
        ]
        if timestamped:
            _, _, row = max(timestamped, key=lambda event: (event[0], event[1]))
        else:
            _, row = events[-1]
        latest[record_id] = row
    return latest
