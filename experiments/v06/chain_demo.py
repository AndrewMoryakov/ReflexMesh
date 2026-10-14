"""A handoff chain through a running `reflexmesh browser serve`, every step a separate CLI call.

Revision 1 of the order-note task lacks the order number, so Jev cannot finish and hands over; revision 2 supplies
it with a clarified goal; if Jev hands over again, this script plays the LLM for at most one assisted step
("Save note"), then returns to Jev. It prints one line per attempt and the final verification.

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
REVISION_2_GOAL = ("The order number is now provided as order@1. Replace the text in the Order number field with "
                   "order@1, search, open order 125, add the prepared note to it and save the note.")


def cli(socket: str, command: str, payload: dict | None = None) -> tuple[int, dict]:
    done = subprocess.run([sys.executable, "-m", "reflexmesh", "browser", command, "--socket", socket,
                           *(["--input", "-"] if payload is not None else [])],
                          input=json.dumps(payload, ensure_ascii=True) if payload is not None else None,
                          capture_output=True, text=True, encoding="ascii", errors="replace")
    return done.returncode, json.loads(done.stdout or "{}")


def brief(tag: str, code: int, r: dict) -> None:
    if r.get("schema_version") == "browser-progress/0.1":
        print(f"{tag:<18} exit {code}  assistance: page {r['observation']['page']}, "
              f"{r['assistance_steps_remaining']} steps left")
    else:
        print(f"{tag:<18} exit {code}  {r.get('attempt_status')}/{r.get('stop_reason')}"
              f"  handoff={(r.get('handoff') or {}).get('reason')}  epoch={r.get('owner_epoch')}"
              f"  chain used={(r.get('chain_budget') or {}).get('used')}" + (f"  error={r['error']}" if "error" in r else ""))


def continuation(r: dict, **fields) -> dict:
    return {"token": r["handoff"]["continuation"]["token"], "parent_attempt_id": r["attempt_id"], **fields}


def resumable(r: dict) -> bool:
    return (r.get("attempt_status") == "incomplete" and
            bool(((r.get("handoff") or {}).get("continuation") or {}).get("allowed")))


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
        code, r = cli(args.socket, "resume", continuation(r, operation="start", mode="jev", task=revision_2))
        brief("A2 jev, rev 2", code, r)
    attempt = 3
    while resumable(r) and attempt <= 5:
        code, r = cli(args.socket, "resume", continuation(r, operation="start", mode="llm"))
        brief(f"A{attempt} llm", code, r)
        attempt += 1
        # The caller in the role of the LLM: one option, chosen by its visible name.
        option = next((o for o in r.get("action_options") or [] if o.get("name") == "Save note"), None)
        if r.get("schema_version") == "browser-progress/0.1" and option is not None:
            code, r = cli(args.socket, "resume", {
                "token": r["continuation_token"], "parent_attempt_id": r["attempt_id"], "operation": "step",
                "proposal": {"observation_id": r["observation"]["observation_id"],
                             "operation": option["operation"], "option_ref": option["ref"]}})
            brief("   step Save note", code, r)
        if r.get("schema_version") == "browser-progress/0.1":
            code, r = cli(args.socket, "resume", {"token": r["continuation_token"],
                                                  "parent_attempt_id": r["attempt_id"], "operation": "return_to_jev"})
            brief("   return_to_jev", code, r)
    for row in r.get("verification") or []:
        print(f"    {row['id']}: {row['status']}")
    return 0 if r.get("attempt_status") == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
