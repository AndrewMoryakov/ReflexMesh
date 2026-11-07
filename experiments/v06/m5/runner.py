"""The comparison protocol of V0.6 section 17: modes J0, J1 and L over one frozen manifest.

    python experiments/v06/m5/runner.py --manifest experiments/v06/m5/manifest.protocol-check.json --out DIR

J0 is Jev alone: a handoff ends the attempt. J1 is Jev with metered help from the LLM after a handoff. L has the LLM
choose every action from the same options. Every planned run gets a row in results.jsonl as soon as it ends, and stays
in the denominator whatever happened. Success is the oracle's, read from the site after the run, and is separate from
what the attempt claimed: `false_completion` is a claim of `completed` the oracle does not confirm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
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
                 "to type a value, one of that option's slot_refs); for an option without a ref, {\"operation\": \"scroll\", "
                 "\"direction\": \"down\" or \"up\"} or {\"operation\": \"wait\"}; or {\"done\": true} when the small model "
                 "can go on or nothing offered helps.")


EXTERNAL_CHECK = "m5.external_oracle"
# What the oracle reads from the live page: the same function for a run and for a reference solution.
FINAL_STATE_JS = """() => {
  const flat = (t) => String(t || '').replace(/\\s+/g, ' ').trim();
  const frames = [], values = [];
  for (const f of document.querySelectorAll('iframe,frame')) {
    try {
      const d = f.contentDocument;
      if (!d || !d.body) continue;
      frames.push(flat(d.body.innerText));
      for (const e of d.querySelectorAll('input,textarea,select')) values.push((e.id || e.name) + '=' + e.value);
    } catch (e) {}
  }
  const words = (node) => [...node.childNodes].map(n => n.nodeType === 3 ? n.textContent :
    n.nodeType === 1 && !['STYLE', 'SCRIPT'].includes(n.tagName) ? words(n) : '').join(' ');
  let shadow = '', checked = 0;
  const walk = (root) => { for (const e of root.querySelectorAll('*')) { if (e.shadowRoot) {
    shadow += ' ' + words(e.shadowRoot); checked += e.shadowRoot.querySelectorAll('input[type=checkbox]:checked').length; walk(e.shadowRoot); } } };
  walk(document);
  return {url: location.href, title: document.title, text: flat(document.body ? document.body.innerText : ''),
          textareas: flat([...document.querySelectorAll('textarea')].map(t => t.value).join(' ')),
          frames, frame_values: values, shadow_text: flat(shadow), shadow_checked: checked};
}"""


def judge_final(oracle: dict, state: dict | None) -> tuple[bool | None, str]:
    """A real site's oracle: the final URL and every expected text in the named part of the final page."""
    if state is None:
        return None, "oracle_unavailable"
    if not re.search(oracle["final_url"], state["url"]):
        return False, "url_differs"
    where = oracle["where"]
    if where == "shadow_checked":
        return (True, "matched") if state["shadow_checked"] else (False, "not_checked")
    bucket = {"page": state["title"] + " " + state["text"], "textarea": state["textareas"], "frame": " ".join(state["frames"]),
              "frame_value": " ".join(state["frame_values"]), "shadow_text": state["shadow_text"]}[where]
    return (True, "matched") if all(text in bucket for text in oracle["contains"]) else (False, "text_missing")


def read_final_state(adapter) -> dict | None:
    """Called in the attempt's own thread after it ended, before the browser may close. Bounded."""
    session = adapter.backend._session

    async def look():
        cdp = await session.get_or_create_cdp_session()
        answer = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": f"({FINAL_STATE_JS})()", "returnByValue": True}, session_id=cdp.session_id)
        return (answer.get("result") or {}).get("value")

    async def bounded():
        return await asyncio.wait_for(look(), 10)

    return adapter.backend._run(bounded())


class ExternalOracle:
    """The criterion of a real-site task: the runtime's verifier is inert here and always answers unknown. It holds
    no source, so `completed` stays out of reach; success belongs to the runner's oracle."""

    check_id = EXTERNAL_CHECK
    claim_id = "none"

    def claim(self, attempt_id, timeout=2.0):
        pass

    def evidence(self, timeout=2.0):
        return {"pending": 0}

    def judge(self, args, expected, evidence):
        return {"status": "unknown", "reason": "judged_by_external_oracle", "facts": {}}

    def settle(self, actions, evidence, *, browser_stopped):
        return None

    def release(self, timeout=1.0):
        pass


def task_data(manifest: dict, spec: dict) -> dict:
    if "task" in spec:
        return json.loads((ROOT / spec["task"]).read_text("utf-8"))
    return {"schema_version": "browser-task/0.1", "task_id": spec["id"], "revision": 1, "goal": spec["goal"],
            "start_url": spec["start_url"], "allowed_origins": spec["allowed_origins"], "permissions": manifest["permissions"],
            "constraints": [], "text_slots": spec["text_slots"], "limits": manifest["limits"],
            "criteria": [{"id": "oracle", "predicate": "registered_check",
                          "args": {"check_id": EXTERNAL_CHECK, "subject": spec["id"], "expected_slot_refs": []}}]}


def remaining(planned: list[dict], done: list[dict]) -> tuple[list[dict], list[dict]]:
    """(finished rows to keep, planned runs still to do). A run that failed on infrastructure is run again."""
    kept = [r for r in done if r.get("category") != "infra"]
    finished = {(r["task"], r["mode"], r["repeat"]) for r in kept}
    return kept, [run for run in planned if (run["task"]["id"], run["mode"], run["repeat"]) not in finished]


def fresh_run_dir(path: Path) -> Path:
    """The directory of one run, empty. A run repeated after an infrastructure failure finds the earlier chain's
    traces there; they move to `<name>.earlier-<n>`, kept but apart, since everything that reads a run (its row, its
    finish, its cost) scans this directory and would mix the two executions (review of PR #15)."""
    if path.exists() and any(path.iterdir()):
        n = 1
        while path.with_name(f"{path.name}.earlier-{n}").exists():
            n += 1
        path.rename(path.with_name(f"{path.name}.earlier-{n}"))
    return path


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
                     "claimed_finish": sum(1 for r in mine if r.get("claimed_finish")),
                     "false_claim": sum(1 for r in mine if r.get("false_claim")),
                     "unknown_effects": sum(r.get("unknown_effects") or 0 for r in mine),
                     "infra_failures": sum(1 for r in mine if r.get("category") == "infra"),
                     "before_assistance": sum(1 for r in mine if r.get("completed_before_assistance")),
                     "wall_p50": percentile(walls, 0.5), "wall_p95": percentile(walls, 0.95),
                     "steps_mean": round(statistics.mean(r.get("steps") or 0 for r in mine), 2) if mine else None,
                     "model_calls_mean": round(statistics.mean(r.get("model_calls") or 0 for r in mine), 2) if mine else None,
                     "assist_calls": sum(r.get("assist_calls") or 0 for r in mine),
                     "cost": round(sum(costs), 6) if mine and all(c is not None for c in costs) else None,
                     "handoffs": {}, "by_group": {}, "oracle_reasons": {}}
        groups: dict = {}
        for r in mine:
            for reason in r.get("handoff_reasons") or []:
                out[mode]["handoffs"][reason] = out[mode]["handoffs"].get(reason, 0) + 1
            if r.get("oracle_reason"):
                out[mode]["oracle_reasons"][r["oracle_reason"]] = out[mode]["oracle_reasons"].get(r["oracle_reason"], 0) + 1
            if r.get("group"):
                good, total = groups.get(r["group"], (0, 0))
                groups[r["group"]] = (good + (r.get("oracle_success") is True), total + 1)
        out[mode]["by_group"] = {g: f"{good}/{total}" for g, (good, total) in sorted(groups.items())}
    return out


def report(manifest: dict, rows: list[dict]) -> str:
    planned = len(plan(manifest))
    lines = [f"kind: {manifest['kind']}", manifest.get("note", ""), f"planned runs: {planned}, rows: {len(rows)}", ""]
    for mode, s in summarize(rows).items():
        lines.append(f"{mode}: confirmed {s['confirmed_success']}/{s['runs']}, claimed completed {s['claimed_completed']}, "
                     f"false completion {s['false_completion']}, claimed finish {s['claimed_finish']}, false claim {s['false_claim']}, "
                     f"by group {s['by_group']}, oracle {s['oracle_reasons']}, unknown effects {s['unknown_effects']}, infra {s['infra_failures']}, "
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

    def _count(self, usage: dict) -> None:
        self.calls += 1
        self.input_tokens += usage["input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.cost = None if self.cost is None or "cost" not in usage else self.cost + usage["cost"]

    def help(self, core, progress: dict) -> dict:
        from systemone_harness.provider import ProviderError

        r = progress
        refused = 0
        while r.get("schema_version") == "browser-progress/0.1" and r["assistance_steps_remaining"] > 0 and refused < 2:
            # Section 12: in the comparison the assistant's requests go through the same model-call counter as Jev's,
            # reserved before sending (review of PR #15: in the M5 run of 2026-11-06 they were not counted).
            if not core.reserve_assist_call(r["attempt_id"]):
                break
            try:
                reply, usage, _, _, _ = self.client.ask(ASSIST_SYSTEM, {
                    "goal": self.goal, "page": r["observation"]["page"], "text": r["observation"].get("text"),
                    "action_options": r["action_options"], "last_proposal": r.get("last_proposal")})
            except ProviderError as exc:
                if getattr(exc, "usage", None) is not None:   # a reply came and was refused: it was still paid for
                    self._count(exc.usage)
                break
            self._count(usage)
            if reply.get("done"):
                break
            proposal = assist_proposal(r, reply)
            if proposal is None:
                break
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


def assist_proposal(progress: dict, reply: dict) -> dict | None:
    """The assistant's reply as a proposal, or None when it names nothing offered. Scroll and wait are offered without a
    ref and are named by their operation; looking them up by ref used to pick scroll for any reply without a ref,
    and to send it without a direction (review of PR #15)."""
    seen = progress["observation"]["observation_id"]
    operation = reply.get("operation")
    unnamed = {o["operation"] for o in progress["action_options"] if o.get("ref") is None}
    if reply.get("option_ref") is None:
        if operation == "wait" and "wait" in unnamed:
            return {"observation_id": seen, "operation": "wait"}
        if operation == "scroll" and "scroll" in unnamed and reply.get("direction") in ("up", "down"):
            return {"observation_id": seen, "operation": "scroll", "direction": reply["direction"]}
        return None
    option = next((o for o in progress["action_options"] if o.get("ref") is not None and o["ref"] == reply["option_ref"]), None)
    if option is None:
        return None
    proposal = {"observation_id": seen, "operation": option["operation"], "option_ref": option["ref"]}
    if reply.get("slot_ref"):
        proposal["slot_ref"] = reply["slot_ref"]
    return proposal


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
    data = task_data(manifest, task_spec)
    local = task_spec.get("site") == "demo"
    final_states: list = []
    row = {"task": task_spec["id"], "group": task_spec["group"], "mode": mode, "repeat": run["repeat"], "category": "ran"}
    attempt_dir = fresh_run_dir(out / f"{task_spec['id']}-{mode}-{run['repeat']}")

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
        if local:
            site = subprocess.Popen([sys.executable, str(ROOT / "experiments/v06/site/server.py"), "--port", "8770"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if not _port_free(8770):
                    break
                time.sleep(0.1)
        checks = load_checks(ROOT / manifest["checks"]) if local else {EXTERNAL_CHECK: ExternalOracle()}
        core = BrowserCore(chrome=chrome, provider_factory=provider_factory, action_provider="chat" if mode == "L" else "jev",
                           out_dir=attempt_dir, sandbox=sandbox, checks=checks, headless=True,
                           final_probe=None if local else (lambda adapter: final_states.append(read_final_state(adapter))))
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
        events = [json.loads(line) for trace in sorted(attempt_dir.rglob("trace.jsonl")) for line in trace.read_text("utf-8").splitlines()]
        decisions = [e for e in events if e["kind"] == "decision_received"]
        row["claimed_finish"] = bool(decisions) and decisions[-1].get("action") == "finish"
        failed = [e for e in events if e["kind"] == "decision_failed" and not e.get("protocol")]
        if failed:      # the provider, not the model: 402 for a spent key, 429, 5xx, a lost connection
            row.update(category="infra", detail=f"provider {failed[-1].get('status')}")
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
        if local:
            ok, why = judge(task_spec["oracle"], data, _site_state(8770))
        else:
            ok, why = judge_final(task_spec["oracle"], final_states[-1] if final_states else None)
            row["final_url"] = (final_states[-1] or {}).get("url") if final_states else None
        row.update(oracle_success=ok, oracle_reason=why, false_completion=row.get("attempt_status") == "completed" and ok is not True,
                   false_claim=bool(row.get("claimed_finish")) and ok is not True)
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
            if event["kind"] == "decision_failed" and event.get("protocol"):
                # A reply refused for its format was still a paid request (review of PR #15). Traces written before
                # the refusal carried its id have nothing to look up: the run's cost is then unknown, not lower.
                if "request_id" not in event:
                    return None
            elif event["kind"] != "decision_received":
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


def key_remaining() -> float | None:
    try:
        request = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
        return json.load(urllib.request.urlopen(request, timeout=20))["data"]["limit_remaining"]
    except (OSError, ValueError, KeyError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text("utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frozen = out / "manifest.json"
    if frozen.exists() and json.loads(frozen.read_text("utf-8")) != manifest:
        raise SystemExit("this output directory holds a different manifest: a frozen set is not changed mid-way")
    frozen.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    commit = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    with (out / "run_info.jsonl").open("a", encoding="utf-8") as info:     # one line per start: a resume on other code shows here
        info.write(json.dumps({"commit": commit, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    chrome = os.environ["REFLEXMESH_CHROME"]
    sandbox = os.environ.get("REFLEXMESH_CHROME_NO_SANDBOX") != "1"
    results = out / "results.jsonl"
    done = [json.loads(line) for line in results.read_text("utf-8").splitlines()] if results.exists() else []
    rows, todo = remaining(plan(manifest), done)
    results.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    uses_demo = any(t.get("site") == "demo" for t in manifest["tasks"])
    stopped = None
    with results.open("a", encoding="utf-8") as sink:
        for run in todo:
            if uses_demo and not _port_free(8770):
                raise SystemExit("port 8770 is busy: nothing was run")
            left = key_remaining()
            if left is not None and left < manifest.get("min_key_remaining", 0.0):
                stopped = f"the key has ${left:.4f} left, below the floor: the remaining runs were not started"
                print(stopped, flush=True)
                break
            row = execute(manifest, run, out, chrome, sandbox)
            rows.append(row)
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            print(row["task"], row["mode"], row["repeat"], row.get("attempt_status"), row.get("stop_reason"), row["oracle_reason"],
                  row.get("final_url") or "", row.get("category"), flush=True)
    for row in rows:      # after every run, not during: the lookups must not count as anyone's wall time
        if row.get("cost") is None:
            row["cost"] = costs_of(out, row)
    results.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    text = report(manifest, rows) + (f"NOT FINISHED: {stopped}\n" if stopped else "")
    (out / "report.txt").write_text(text, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
