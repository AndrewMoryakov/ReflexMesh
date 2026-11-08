"""Record browser steps for stage 1 of docs/specs/V0.6-focus-region.md (see PROTOCOL-stage1.md).

Runs BrowserRun with a scripted provider that also records, per step, the SystemOneHarness request as sent,
the right answer the script chose, and the raw container chain of every offered option. Experiment code: the
adapter is observed through two wrappers installed here, not changed.

  python record_steps.py --tasks tasks/books.json --out steps/ [--explore]

A task file is a JSON list of {"task": {...}, "steps": [...]}; `task` takes the fields of browser-task/0.1 that
differ from DEFAULTS. A step is a LabelScriptProvider step, plus an optional `within`: {"<parameter>": regex}
that the option's container chain must match when its description alone matches several options.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src")]

from reflexmesh.adapters.system_one import browser_adapter as BA  # noqa: E402
from reflexmesh.adapters.system_one.decision_providers import (  # noqa: E402
    LabelScriptProvider, ScriptMismatch, _choice, answers_for_step)
from reflexmesh.contracts.browser import BrowserTask  # noqa: E402
from reflexmesh.runtime.browser_run import BrowserRun  # noqa: E402

PERMISSIONS = ["navigate", "click", "type_text", "select_option", "scroll", "go_back", "switch_tab", "wait"]
DEFAULTS = {
    "schema_version": "browser-task/0.1", "revision": 1, "permissions": PERMISSIONS, "constraints": [],
    "limits": {"wall_seconds": 240, "max_steps": 40, "max_model_calls": 40, "max_action_retries": 0, "max_handoffs": 0},
}
RECORDING_CHECK = "focus.recording"
CHAIN_DEPTH = 12
TEXT_CHARS = 120
ELEMENT, DOCUMENT, FRAGMENT = 1, 9, 11
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

LIVE: dict = {"adapter": None, "nodes": {}}
_init, _capture = BA.GenericBrowserAdapter.__init__, BA.GenericBrowserAdapter._capture


def _wrapped_init(self, *a, **k):
    _init(self, *a, **k)
    LIVE["adapter"] = self


async def _wrapped_capture(self):
    result = await _capture(self)
    LIVE["nodes"] = result[2] or {}
    return result


BA.GenericBrowserAdapter.__init__ = _wrapped_init
BA.GenericBrowserAdapter._capture = _wrapped_capture


class RecordingCheck:
    """The criterion of a recording task: inert, always unknown. Recording judges nothing; the script is the label."""

    check_id = RECORDING_CHECK
    claim_id = "none"

    def claim(self, attempt_id, timeout=2.0):
        pass

    def evidence(self, timeout=2.0):
        return {"pending": 0}

    def judge(self, args, expected, evidence):
        return {"status": "unknown", "reason": "recording_only", "facts": {}}

    def settle(self, actions, evidence, *, browser_stopped):
        return None

    def release(self, timeout=1.0):
        pass


def _text(node, depth=4) -> str:
    try:
        return " ".join(node.get_all_children_text(max_depth=depth).split())[:TEXT_CHARS]
    except Exception:  # a node Browser Use cannot walk is recorded without text
        return ""


def _first_heading(node, depth=4) -> str | None:
    stack = [(node, 0)]
    while stack:
        cur, d = stack.pop(0)
        if cur is not node and BA._node_kind(cur) == ELEMENT:
            role = str(getattr(getattr(cur, "ax_node", None), "role", "") or "").lower()
            if cur.tag_name in HEADING_TAGS or role == "heading":
                return _text(cur, 3) or None
        if d < depth:
            stack.extend((c, d + 1) for c in (cur.children_nodes or []))
    return None


def container_chain(node) -> list[dict]:
    """Ancestors of a node up to its document or shadow root, raw."""
    chain, cur = [], node.parent_node
    while cur is not None and len(chain) < CHAIN_DEPTH:
        kind = BA._node_kind(cur)
        if kind in (DOCUMENT, FRAGMENT):
            chain.append({"boundary": "shadow_root" if kind == FRAGMENT else "document"})
            break
        if kind == ELEMENT:
            attrs = cur.attributes or {}
            ax = getattr(cur, "ax_node", None)
            parent = cur.parent_node
            same = [c for c in (parent.children_nodes or []) if BA._node_kind(c) == ELEMENT
                    and c.tag_name == cur.tag_name] if parent is not None else [cur]
            entry = {"tag": cur.tag_name, "role": str(getattr(ax, "role", "") or "") or None,
                     "name": (str(getattr(ax, "name", "") or "")[:TEXT_CHARS] or None),
                     "id": attrs.get("id"), "aria_label": attrs.get("aria-label"),
                     "pos": next((i + 1 for i, c in enumerate(same) if c is cur), None), "of": len(same)}
            if cur.tag_name not in ("html", "body"):
                entry["text"] = _text(cur)
                entry["heading"] = _first_heading(cur)
            chain.append(entry)
            if cur.tag_name == "body":
                break
        cur = cur.parent_node
    return chain


def within_depth(chain: list[dict], pattern) -> int | None:
    """How far up the chain the first container matching `pattern` is, or None."""
    for depth, e in enumerate(chain):
        if pattern.search(" ".join(str(e.get(k) or "") for k in ("name", "aria_label", "heading", "text"))):
            return depth
    return None


def chain_text(chain: list[dict]) -> str:
    return " | ".join(" ".join(str(e.get(k) or "") for k in ("name", "aria_label", "heading", "text")) for e in chain)


class Recorder(LabelScriptProvider):
    model = "script/record"

    def __init__(self, steps, sink, task_id: str, explore: bool):
        super().__init__(steps)
        self.sink, self.task_id, self.explore = sink, task_id, explore

    def _containers(self, questions: dict) -> tuple[dict, int]:
        adapter, nodes = LIVE["adapter"], LIVE["nodes"]
        by_key = {t.key: t for t in (getattr(adapter, "_table", {}) or {}).values() if getattr(t, "key", None)}
        out, unmapped = {}, 0
        for q in questions.values():
            for key in (q.get("criteria") or {}) if q.get("type") == "choice" else ():
                if key in out:
                    continue
                t = by_key.get(key)
                node = nodes.get(t.index) if t is not None and t.index is not None else None
                if node is None:
                    unmapped += t is not None and t.index is not None
                    continue
                out[key] = container_chain(node)
        return out, unmapped

    def decide(self, state, questions: dict):
        from systemone_harness.provider import Decision, ProviderError

        step = self.steps[self.calls] if self.calls < len(self.steps) else {"action": "finish"}
        self.calls += 1
        containers, unmapped = self._containers(questions)
        adapter = LIVE["adapter"]
        row = {"task_id": self.task_id, "step": self.calls, "recorded_at": time.time(),
               "url": (getattr(adapter, "report", {}) or {}).get("url"),
               "signature": (getattr(adapter, "report", {}) or {}).get("signature"),
               "state": state, "questions": questions, "script_step": step,
               "containers": containers, "containers_unmapped": unmapped}
        try:
            answers = self._answers(step, questions, containers)
        except ScriptMismatch as exc:
            row["mismatch"] = str(exc)
            self.sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.sink.flush()
            raise ProviderError(f"script: {exc} at step {self.calls}") from None
        action = step.get("action")
        row["gold"] = {k: (a.pop("equivalent", None) or [a.get("choice")]) for k, a in answers.items()
                       if a.get("type") == "choice" and (k == "next_action" or k.startswith(f"{action}__"))}
        self.sink.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.sink.flush()
        return Decision(answers=answers, usage={"input_tokens": 0, "output_tokens": 0}, model=self.model,
                        request_id=f"record-{self.calls}", latency_ms=1, raw={"script_step": self.calls})

    @staticmethod
    def _answers(step: dict, questions: dict, containers: dict) -> dict:
        within = step.get("within") or {}
        if not within and not step.get("any"):
            return answers_for_step(step, questions)
        plain = {k: v for k, v in step.items() if k not in ("within", "any") and k not in within}
        if step.get("any"):
            plain = {k: v for k, v in plain.items() if k in ("action",)}
        answers = answers_for_step(plain, questions)
        action = step["action"]
        for key, question in questions.items():
            parameter = key.split("__", 1)[1] if "__" in key else key
            if not key.startswith(f"{action}__") or parameter not in step or parameter == "action":
                continue
            criteria = question.get("criteria") or {}
            label = re.compile(str(step[parameter]), re.IGNORECASE)
            inside = re.compile(str(within.get(parameter, "")), re.IGNORECASE)
            matches = [ref for ref, d in criteria.items() if label.search(str(d))]
            if parameter in within:
                # the nearest container wins: a book's own card matches its title before the list around all books
                depth = {ref: within_depth(containers.get(ref) or [], inside) for ref in matches}
                found = [d for d in depth.values() if d is not None]
                matches = [ref for ref in matches if found and depth[ref] == min(found)]
            if not matches or (len(matches) > 1 and not step.get("any")):
                raise ScriptMismatch(f"{parameter!r} within {within.get(parameter)!r} matched {len(matches)} options")
            answers[key] = _choice(criteria, matches[0])
            answers[key]["equivalent"] = matches
        return answers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", help="task_id to run")
    ap.add_argument("--explore", action="store_true", help="dump offered options; not part of the set")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    chrome = os.environ["REFLEXMESH_CHROME"]
    args.out.mkdir(parents=True, exist_ok=True)
    specs = json.loads(args.tasks.read_text("utf-8"))
    for spec in specs:
        data = {**DEFAULTS, "text_slots": [], **spec["task"]}
        data.setdefault("criteria", [{"id": "recording", "predicate": "registered_check",
                                      "args": {"check_id": RECORDING_CHECK, "subject": data["task_id"],
                                               "expected_slot_refs": []}}])
        if args.only and data["task_id"] != args.only:
            continue
        tag = "explore" if args.explore else "steps"
        path = args.out / f"{data['task_id']}.{tag}.jsonl"
        with path.open("w", encoding="utf-8") as sink:
            recorder = Recorder(spec.get("steps") or [], sink, data["task_id"], args.explore)
            run = BrowserRun(BrowserTask.from_dict(data), chrome=chrome,
                             provider_factory=lambda: (recorder, "script:record", None), action_provider="script",
                             out_dir=args.out / "runs" / f"{data['task_id']}.{tag}", headless=not args.headed,
                             checks={RECORDING_CHECK: RecordingCheck()})
            result = run.run()
        rows = [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]
        print(json.dumps({"task_id": data["task_id"], "status": result.get("attempt_status"),
                          "stop": result.get("stop_reason"), "steps": len(rows),
                          "mismatch": next((r["mismatch"] for r in rows if "mismatch" in r), None),
                          "unmapped": sum(r["containers_unmapped"] for r in rows),
                          "options_with_chain": sum(len(r["containers"]) for r in rows)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
