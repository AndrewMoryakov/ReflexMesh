"""Resolve action effects and repeat evidence from an authenticated fixture record.

The record is the retained `fixture_effect_snapshot` that FixtureVerifier builds from the
fixture's own authenticated response: effects bound to one claim since the baseline, and the
count of effects still pending. It is the only source here that sees what the browser driver
actually did, including repeats inside the driver.
"""

from __future__ import annotations

# Mutating targets whose click is a POST the fixture records, and the path it is recorded under.
TARGET_PATHS = {"send-form": "/form", "send-support": "/support", "save-settings": "/settings",
                "start-export": "/slow/export", "delete-account": "/danger/delete"}


def valid_snapshot(value) -> bool:
    if (type(value) is not dict or value.get("kind") != "fixture_effect_snapshot" or
            value.get("source") != "authenticated_fixture_response" or
            type(value.get("ref")) is not str or not value["ref"]):
        return False
    count = value.get("pending_effect_count")
    if type(count) is not int or count < 0 or type(value.get("effects")) is not list:
        return False
    seqs = [effect.get("seq") for effect in value["effects"] if type(effect) is dict]
    return (len(seqs) == len(value["effects"]) and all(type(seq) is int for seq in seqs) and
            len(set(seqs)) == len(seqs) and
            all(type(effect.get("path")) is str for effect in value["effects"]))


def snapshot_of(verification) -> dict | None:
    """The first well-formed retained snapshot among verification rows, if any."""
    if type(verification) is not list:
        return None
    for row in verification:
        if type(row) is dict and valid_snapshot(row.get("fixture_snapshot")):
            return row["fixture_snapshot"]
    return None


def _counts(snapshot: dict) -> dict:
    counts: dict = {}
    for effect in snapshot["effects"]:
        counts[effect["path"]] = counts.get(effect["path"], 0) + 1
    return counts


def _rows_by_path(actions) -> dict:
    grouped: dict = {}
    for row in actions:
        path = TARGET_PATHS.get(row.get("target_id"))
        if path is not None and row.get("operation") != "navigate":
            grouped.setdefault(path, []).append(row)
    return grouped


def resolve_effects(actions: list, snapshot: dict | None, same_owner, *, browser_stopped: bool) -> None:
    """Settle `unknown` effects of mutating rows from the server record; never unsettle one.

    Every request the fixture recorded for a target is matched against the rows committed
    for it. Equal counts with nothing pending mean each row was requested exactly once. No
    request, nothing pending and a browser proven stopped means none can still arrive.
    Anything else stays unknown.
    """
    if snapshot is None or snapshot["pending_effect_count"]:
        return
    counts = _counts(snapshot)
    for path, rows in _rows_by_path(actions).items():
        if any(not same_owner(row.get("ownership"), snapshot.get("ownership")) for row in rows):
            continue
        pending = [row for row in rows if row.get("effect") == "unknown"]
        requests = counts.get(path, 0)
        if not pending:
            continue
        if requests == len(rows):
            for row in pending:
                row["effect"] = "applied"
                row["evidence_refs"] = [snapshot["ref"]]
        elif requests == 0 and browser_stopped:
            for row in pending:
                row["effect"] = "not_applied"
                row["evidence_refs"] = [snapshot["ref"]]


def repeat_verdict(actions: list, snapshot: dict | None) -> tuple[str, str] | None:
    """("fail"|"pass", reason) when the record decides repeats, None when it cannot.

    More requests than committed rows for a target is a confirmed repeat, even one the
    driver made internally. A pass needs every target's requests to equal its committed rows,
    nothing pending, and no request for a target nothing was committed for.
    """
    if snapshot is None or snapshot["pending_effect_count"]:
        return None
    counts = _counts(snapshot)
    grouped = _rows_by_path(actions)
    if any(counts.get(path, 0) > len(rows) for path, rows in grouped.items()):
        return "fail", "The fixture recorded more requests for a target than mutations were committed for it."
    if any(path in counts and path not in grouped for path in TARGET_PATHS.values()):
        return None
    if all(counts.get(path, 0) == len(rows) for path, rows in grouped.items()):
        return "pass", ("The authenticated fixture record shows each committed server-visible mutation "
                        "requested exactly once and none pending; typing has no server effect.")
    return None
