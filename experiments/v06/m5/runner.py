"""The comparison protocol of V0.6 section 17: modes J0, J1 and L over one frozen manifest.

    python experiments/v06/m5/runner.py --manifest experiments/v06/m5/manifest.protocol-check.json --out DIR

J0 is Jev alone: a handoff ends the attempt. J1 is Jev with metered help from the LLM after a handoff. L has the LLM
choose every action from the same options. Every planned run gets a row in results.jsonl as soon as it ends, and stays
in the denominator whatever happened. Success is the oracle's, read from the site after the run, and is separate from
what the attempt claimed: `false_completion` is a claim of `completed` the oracle does not confirm.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODES = ("J0", "J1", "L")
ASSIST_SYSTEM = ("You help a small browser model that stopped and asked for help. Choose one next step toward the goal "
                 "from `action_options`. Reply with one JSON object: {\"option_ref\": ..., \"slot_ref\": ...} (slot_ref only "
                 "to type a value, one of that option's slot_refs), or {\"done\": true} when the small model can go on or "
                 "nothing offered helps.")


def plan(manifest: dict) -> list[dict]:
    """Every planned run, in order. The modes rotate per repeat, so none always runs first or last."""
    runs = []
    for repeat in range(1, manifest["repeats"] + 1):
        order = MODES[(repeat - 1) % 3:] + MODES[:(repeat - 1) % 3]
        for task in manifest["tasks"]:
            runs.extend({"task": task, "mode": mode, "repeat": repeat} for mode in order)
    return runs


def judge(oracle: dict, task: dict, state: dict | None) -> tuple[bool | None, str]:
    """The oracle knows the site's data; it never reached the control loop."""
    if state is None:
        return None, "oracle_unavailable"
    if oracle["kind"] != "order_desk_note":
        raise ValueError(oracle["kind"])
    want = next(s["value"] for s in task["text_slots"] if s["id"] == oracle["note_slot"])
    notes = state["notes"].get(oracle["order"], [])
    if notes == [want]:
        return True, "one_correct_note"
    return False, "no_note" if not notes else "wrong_notes"


def percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def summarize(rows: list[dict]) -> dict:
    """Per mode, over every planned run. Cost is None unless known for every run: unknown cost is no saving."""
    out = {}
    for mode in MODES:
        mine = [r for r in rows if r["mode"] == mode]
        walls = [r["wall_seconds"] for r in mine if r.get("wall_seconds") is not None]
        costs = [r.get("cost") for r in mine]
        out[mode] = {"runs": len(mine),
                     "confirmed_success": sum(1 for r in mine if r.get("oracle_success") is True),
                     "claimed_completed": sum(1 for r in mine if r.get("attempt_status") == "completed"),
                     "false_completion": sum(1 for r in mine if r.get("false_completion")),
                     "unknown_effects": sum(r.get("unknown_effects") or 0 for r in mine),
                     "infra_failures": sum(1 for r in mine if r.get("category") == "infra"),
                     "before_assistance": sum(1 for r in mine if r.get("completed_before_assistance")),
                     "wall_p50": percentile(walls, 0.5), "wall_p95": percentile(walls, 0.95),
                     "steps_mean": round(statistics.mean(r.get("steps") or 0 for r in mine), 2) if mine else None,
                     "model_calls_mean": round(statistics.mean(r.get("model_calls") or 0 for r in mine), 2) if mine else None,
                     "assist_calls": sum(r.get("assist_calls") or 0 for r in mine),
                     "cost": round(sum(costs), 6) if mine and all(c is not None for c in costs) else None,
                     "handoffs": {}}
        for r in mine:
            for reason in r.get("handoff_reasons") or []:
                out[mode]["handoffs"][reason] = out[mode]["handoffs"].get(reason, 0) + 1
    return out


def report(manifest: dict, rows: list[dict]) -> str:
    planned = len(plan(manifest))
    lines = [f"kind: {manifest['kind']}", manifest.get("note", ""), f"planned runs: {planned}, rows: {len(rows)}", ""]
    for mode, s in summarize(rows).items():
        lines.append(f"{mode}: confirmed {s['confirmed_success']}/{s['runs']}, claimed completed {s['claimed_completed']}, "
                     f"false completion {s['false_completion']}, unknown effects {s['unknown_effects']}, infra {s['infra_failures']}, "
                     f"before assistance {s['before_assistance']}, wall p50 {s['wall_p50']} p95 {s['wall_p95']}, steps mean {s['steps_mean']}, "
                     f"model calls mean {s['model_calls_mean']}, assist calls {s['assist_calls']}, cost {s['cost']}, handoffs {s['handoffs']}")
    return "\n".join(lines) + "\n"


def _port_free(port: int) -> bool:
    import socket

    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _site_state(port: int) -> dict | None:
    try:
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/__state", timeout=5))
    except (OSError, ValueError):
        return None


class Assistant:
    """J1's LLM, the caller's role: at most the steps a handoff allows, through the same gate."""

    def __init__(self, client, goal: str):
        self.client, self.goal = client, goal
        self.calls, self.input_tokens, self.output_tokens, self.steps = 0, 0, 0, 0
        self.cost: float | None = 0.0

    def help(self, core, progress: dict) -> dict:
        from systemone_harness.provider import ProviderError

        r = progress
        refused = 0
        while r.get("schema_version") == "browser-progress/0.1" and r["assistance_steps_remaining"] > 0 and refused < 2:
            try:
                reply, usage, _, _, _ = self.client.ask(ASSIST_SYSTEM, {
                    "goal": self.goal, "page": r["observation"]["page"], "text": r["observation"].get("text"),
                    "action_options": r["action_options"], "last_proposal": r.get("last_proposal")})
            except ProviderError:
                break
            self.calls += 1
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
            self.cost = None if self.cost is None or "cost" not in usage else self.cost + usage["cost"]
            option = next((o for o in r["action_options"] if o["ref"] == reply.get("option_ref")), None)
            if reply.get("done") or option is None:
                break
            proposal = {"observation_id": r["observation"]["observation_id"], "operation": option["operation"], "option_ref": option["ref"]}
            if reply.get("slot_ref"):
                proposal["slot_ref"] = reply["slot_ref"]
            r = core.resume({"token": r["continuation_token"], "parent_attempt_id": r["attempt_id"], "operation": "step", "proposal": proposal})
            if r.get("schema_version") == "browser-progress/0.1":
                dispatched = (r.get("last_proposal") or {}).get("dispatched")
                self.steps += 1 if dispatched else 0
                refused += 0 if dispatched else 1
            elif r.get("schema_version") == "browser-result/0.1":
                self.steps += 1      # the step that ended the attempt
        if r.get("schema_version") == "browser-progress/0.1":
            r = core.resume({"token": r["continuation_token"], "parent_attempt_id": r["attempt_id"], "operation": "return_to_jev"})
        return r


def _resumable(r: dict) -> bool:
    return (r.get("attempt_status") == "incomplete" and
            bool(((r.get("handoff") or {}).get("continuation") or {}).get("allowed")))


def execute(manifest: dict, run: dict, out: Path, chrome: str, sandbox: bool) -> dict:
    from reflexmesh.adapters.system_one.decision_providers import (
        ChatClient, ProviderUnavailable, chat_provider_from_env, jev_provider_from_env)
    from reflexmesh.contracts.browser import BrowserTask
    from reflexmesh.runtime.browser_core import BrowserCore
    from reflexmesh.verification.checks import load_checks

    task_spec, mode = run["task"], run["mode"]
    data = json.loads((ROOT / task_spec["task"]).read_text("utf-8"))
    row = {"task": task_spec["id"], "group": task_spec["group"], "mode": mode, "repeat": run["repeat"], "category": "ran"}
    attempt_dir = out / f"{task_spec['id']}-{mode}-{run['repeat']}"

    def provider_factory():
        if mode == "L":
            provider, config = chat_provider_from_env(manifest["llm_model"])
        else:
            provider, config = jev_provider_from_env(manifest["jev_model"])
        return provider, config, provider.model

    started = time.monotonic()
    site = core = None
    assistant = Assistant(ChatClient(os.environ["OPENROUTER_API_KEY"], manifest["llm_model"]), data["goal"]) if mode == "J1" else None
    results = []
    try:
        site = subprocess.Popen([sys.executable, str(ROOT / "experiments/v06/site/server.py"), "--port", "8770"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if not _port_free(8770):
                break
            time.sleep(0.1)
        core = BrowserCore(chrome=chrome, provider_factory=provider_factory, action_provider="chat" if mode == "L" else "jev",
                           out_dir=attempt_dir, sandbox=sandbox, checks=load_checks(ROOT / manifest["checks"]), headless=True)
        r = core.execute(BrowserTask.from_dict(data))
        results.append(r)
        if r.get("attempt_status") == "completed":
            row["completed_before_assistance"] = True
        while mode == "J1" and _resumable(r):
            cont = r["handoff"]["continuation"]
            progress = core.resume({"token": cont["token"], "parent_attempt_id": r["attempt_id"], "operation": "start", "mode": "llm"})
            if progress.get("schema_version") != "browser-progress/0.1":
                r = progress
            else:
                r = assistant.help(core, progress)
            if r.get("schema_version") == "browser-result/0.1":
                results.append(r)
            else:
                break
        last = results[-1]
        row.update(attempt_status=last.get("attempt_status"), stop_reason=last.get("stop_reason"), attempts=len(results),
                   steps=sum(x["budget"]["steps"] for x in results), model_calls=sum(x["budget"]["model_calls"] for x in results),
                   handoff_reasons=[(x.get("handoff") or {}).get("reason") for x in results if x.get("handoff")],
                   unknown_effects=len({a["id"] for a in (last.get("actions") or []) if a["effect"] == "unknown" and a["operation"] != "navigate"}),
                   served_models=sorted({m for x in results for m in (x["decision"].get("served_models") or [])}))
    except ProviderUnavailable as exc:
        row.update(category="infra", detail=str(exc))
    except Exception as exc:      # the row stays: an infra failure is in the denominator with its category
        row.update(category="infra", detail=type(exc).__name__)
    finally:
        row["wall_seconds"] = round(time.monotonic() - started, 3)
        if core is not None:
            try:
                core.close()
            except Exception:
                pass
        ok, why = judge(task_spec["oracle"], data, _site_state(8770))
        row.update(oracle_success=ok, oracle_reason=why, false_completion=row.get("attempt_status") == "completed" and ok is not True)
        if site is not None:
            import psutil

            try:
                for child in [*psutil.Process(site.pid).children(recursive=True), psutil.Process(site.pid)]:
                    child.kill()
            except psutil.Error:
                pass
            site.wait(timeout=10)
        if assistant is not None:
            row.update(assist_calls=assistant.calls, assist_steps=assistant.steps, assist_input_tokens=assistant.input_tokens,
                       assist_output_tokens=assistant.output_tokens, assist_cost=assistant.cost)
            assistant.client.close()
    return row


def costs_of(out: Path, row: dict) -> float | None:
    """What this run's model requests cost, from each request's own record. None if any stays unknown."""
    key, total = os.environ["OPENROUTER_API_KEY"], 0.0
    attempt_dir = out / f"{row['task']}-{row['mode']}-{row['repeat']}"
    traces = list(attempt_dir.rglob("trace.jsonl"))
    if not traces:
        return None
    for trace in traces:
        for line in trace.read_text("utf-8").splitlines():
            event = json.loads(line)
            if event["kind"] != "decision_received":
                continue
            if type((event.get("usage") or {}).get("cost")) in (int, float):
                total += event["usage"]["cost"]
                continue
            cost = None
            for delay in (0, 5, 15, 30):    # a request's record appears some time after the request
                time.sleep(delay)
                try:
                    request = urllib.request.Request(f"https://openrouter.ai/api/v1/generation?id={event['request_id']}",
                                                     headers={"Authorization": f"Bearer {key}"})
                    cost = json.load(urllib.request.urlopen(request, timeout=20))["data"]["total_cost"]
                    break
                except (OSError, ValueError, KeyError):
                    continue
            if cost is None:
                return None
            total += cost
    if row["mode"] == "J1":
        return None if row.get("assist_cost") is None else total + row["assist_cost"]
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text("utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    chrome = os.environ["REFLEXMESH_CHROME"]
    sandbox = os.environ.get("REFLEXMESH_CHROME_NO_SANDBOX") != "1"
    rows = []
    with (out / "results.jsonl").open("a", encoding="utf-8") as sink:
        for run in plan(manifest):
            if not _port_free(8770):
                raise SystemExit("port 8770 is busy: nothing was run")
            row = execute(manifest, run, out, chrome, sandbox)
            rows.append(row)
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            print(row["task"], row["mode"], row["repeat"], row.get("attempt_status"), row.get("stop_reason"), row["oracle_reason"], flush=True)
    for row in rows:      # after every run, not during: the lookups must not count as anyone's wall time
        row["cost"] = costs_of(out, row)
    (out / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    text = report(manifest, rows)
    (out / "report.txt").write_text(text, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
