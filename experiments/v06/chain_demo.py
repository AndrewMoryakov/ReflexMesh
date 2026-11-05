"""A handoff chain through a running `reflexmesh browser serve`, every step a separate CLI call.

Revision 1 of the order-note task lacks the order number, so Jev cannot finish and hands over; revision 2 supplies
it and asks for a restart from start_url, since the field still holds the wrong value; if Jev hands over again, this script stands in for the calling LLM: it takes the remaining
steps from wherever the browser stopped, by the visible names of the offered options, then returns to Jev. It is a
script, not a model: it shows the assisted path, not that an LLM would choose these steps. It prints one line per
attempt and per assisted step, then the final verification.

    python experiments/v06/chain_demo.py --socket STATE/svc.sock [--task experiments/v06/tasks/order-note-verified.json]

The service must already run (`reflexmesh browser serve --socket ... --checks experiments/v06/checks/order-desk.json`)
and the demo site must listen where the task points.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVISION_2_GOAL = "Find order 125 (order number order@1) in the order desk, add the prepared note to it and save the note."


def cli(socket: str, command: str, payload: dict | None = None) -> tuple[int, dict]:
    done = subprocess.run([sys.executable, "-m", "reflexmesh", "browser", command, "--socket", socket,
                           *(["--input", "-"] if payload is not None else [])],
                          input=json.dumps(payload, ensure_ascii=True) if payload is not None else None,
                          capture_output=True, text=True, encoding="ascii", errors="replace")
    return done.returncode, json.loads(done.stdout or "{}")


def brief(tag: str, code: int, r: dict) -> None:
    if r.get("schema_version") == "browser-progress/0.1":
        rejected = (r.get("last_proposal") or {}).get("rejected")
        print(f"{tag:<18} exit {code}  assistance: page {r['observation']['page']}, "
              f"{r['assistance_steps_remaining']} steps left" + (f", rejected: {rejected}" if rejected else ""))
    else:
        print(f"{tag:<18} exit {code}  {r.get('attempt_status')}/{r.get('stop_reason')}"
              f"  handoff={(r.get('handoff') or {}).get('reason')}  epoch={r.get('owner_epoch')}"
              f"  chain used={(r.get('chain_budget') or {}).get('used')}" + (f"  error={r['error']}" if "error" in r else ""))


def continuation(r: dict, **fields) -> dict:
    return {"token": r["handoff"]["continuation"]["token"], "parent_attempt_id": r["attempt_id"], **fields}


def resumable(r: dict) -> bool:
    return (r.get("attempt_status") == "incomplete" and
            bool(((r.get("handoff") or {}).get("continuation") or {}).get("allowed")))


def next_step(progress: dict, used: set[str]) -> tuple[dict, str | None] | None:
    """The next assisted step for this page as (option, slot reference), each named at most once; None when none fits.
    The goal's order: find the order, open it, type the note, save. A result link's name can be the masked slot."""
    slots = progress.get("text_slot_refs") or []

    def offered(operation: str, *names: str) -> dict | None:
        return next((o for o in progress.get("action_options") or []
                     if o.get("operation") == operation and o.get("name") in names and o["name"] not in used), None)

    if (note := offered("type_text", "Note")) and "note@1" in slots:
        return note, "note@1"
    if save := offered("click", "Save note"):
        return save, None
    if order := offered("click", "Заказ №125", "[slot:record@1]"):
        return order, None
    if (field := offered("type_text", "Order number")) and "order@1" in slots:
        return field, "order@1"
    if "Order number" in used and (search := offered("click", "Search")):
        return search, None
    return None


def assist(resume, progress: dict, tag: str = "   step") -> dict:
    """Take steps until the attempt ends or none fits, then hand back to Jev. `resume` sends one continuation."""
    used: set[str] = set()
    r = progress
    while r.get("schema_version") == "browser-progress/0.1" and (step := next_step(r, used)):
        option, slot = step
        used.add(option["name"])
        code, r = resume({"token": r["continuation_token"], "parent_attempt_id": r["attempt_id"], "operation": "step",
                          "proposal": {"observation_id": r["observation"]["observation_id"],
                                       "operation": option["operation"], "option_ref": option["ref"],
                                       **({"slot_ref": slot} if slot else {})}})
        brief(f"{tag} {option['operation']}", code, r)
    if r.get("schema_version") == "browser-progress/0.1":
        code, r = resume({"token": r["continuation_token"], "parent_attempt_id": r["attempt_id"],
                          "operation": "return_to_jev"})
        brief("   return_to_jev", code, r)
    return r


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--socket", required=True)
    parser.add_argument("--task", default=str(ROOT / "experiments/v06/tasks/order-note-verified.json"))
    args = parser.parse_args()
    base = json.loads(Path(args.task).read_text("utf-8"))
    revision_1 = dict(base, text_slots=[s for s in base["text_slots"] if s["id"] != "order"])
    revision_2 = dict(base, revision=int(base.get("revision", 1)) + 1, goal=REVISION_2_GOAL)

    code, r = cli(args.socket, "submit", revision_1)
    brief("A1 jev", code, r)
    if resumable(r):
        code, r = cli(args.socket, "resume", continuation(r, operation="start", mode="jev", task=revision_2, restart=True))
        brief("A2 jev, rev 2", code, r)
    attempt = 3
    while resumable(r) and attempt <= 5:
        code, r = cli(args.socket, "resume", continuation(r, operation="start", mode="llm"))
        brief(f"A{attempt} script as LLM", code, r)
        attempt += 1
        r = assist(lambda payload: cli(args.socket, "resume", payload), r)
    for row in r.get("verification") or []:
        print(f"    {row['id']}: {row['status']}")
    return 0 if r.get("attempt_status") == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
