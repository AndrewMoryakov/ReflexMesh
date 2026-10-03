"""Narrow Browser Use adapter for the isolated V0.5 fixture, loaded only when installed.

Operation identity comes from trusted fixture definitions (element kind, link destination, form
endpoint) checked against live DOM properties, never from labels. Every dependency-specific
access (selector map, CDP frame tree, CDP node properties) is isolated in this module and was
checked against Browser Use 0.13.10 on Chromium 153.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import uuid
from urllib.parse import urljoin, urlsplit

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.faults import sleep_forever
from reflexmesh.runtime.runner import RuntimeStop

OPERATIONS = {
    "home": "navigate", "profile": "navigate", "settings": "navigate",
    "reports": "navigate", "form": "navigate", "danger": "navigate", "export": "navigate",
    "notify-email": "toggle_setting", "save-settings": "save_settings",
    "name": "type_text", "email": "type_text", "send-form": "submit_form",
    "start-export": "start_export", "delete-account": "delete_account",
    "support-name": "type_text", "support-email": "type_text", "support-message": "type_text",
    "send-support": "submit_form",
}
PATHS = {"/", "/profile", "/reports", "/settings", "/form", "/danger", "/slow", "/support"}
LINKS = {"home": "/", "profile": "/profile", "settings": "/settings", "reports": "/reports",
         "form": "/form", "danger": "/danger", "export": "/slow"}
# Trusted fixture definitions: (tag, type attribute, form endpoint path, form method).
CONTROLS = {
    "notify-email": ("input", "checkbox", "/settings", "post"),
    "save-settings": ("button", "submit", "/settings", "post"),
    "name": ("input", "text", "/form", "post"),
    "email": ("input", "email", "/form", "post"),
    "send-form": ("button", "submit", "/form", "post"),
    "start-export": ("button", "submit", "/slow/export", "post"),
    "delete-account": ("button", "submit", "/danger/delete", "post"),
    "support-name": ("input", "text", "/support", "post"),
    "support-email": ("input", "email", "/support", "post"),
    "support-message": ("textarea", "", "/support", "post"),
    "send-support": ("button", "submit", "/support", "post"),
}
PROFILE_PREFIX = "browser-use-user-data-dir-reflexmesh-"  # Browser Use keeps such a profile as is.

# Evaluated on one node resolved by backend node ID; returns only identity and policy facts.
_NODE_FACTS = """function () {
  var base = document.baseURI;
  function abs(v) { try { return v == null ? null : new URL(v, base).href; } catch (e) { return "invalid"; } }
  var rid = this.getAttribute ? this.getAttribute("data-reflex-id") : null;
  var form = this.form || null, action = null, method = null;
  if (form) {
    action = abs((this.getAttribute("formaction")) || form.getAttribute("action") || base);
    method = ((this.getAttribute("formmethod")) || form.getAttribute("method") || "get").toLowerCase();
  }
  return {connected: this.isConnected, reflex_id: rid, tag: (this.tagName || "").toLowerCase(),
          type: ((this.getAttribute && this.getAttribute("type")) || "").toLowerCase(),
          href: this.tagName === "A" ? abs(this.getAttribute("href")) : null,
          disabled: !!(this.disabled || (this.matches && this.matches(":disabled"))),
          form_action: action, form_method: method,
          duplicates: rid ? document.querySelectorAll('[data-reflex-id="' + CSS.escape(rid) + '"]').length : 0,
          top_frame: window === window.top,
          required: !!this.required,
          empty: ("value" in this) ? String(this.value || "").trim() === "" : null};
}"""


class _RedactSlots(logging.Filter):
    """Browser Use logs typed text at info level; ordinary logs must never carry slot values."""

    def __init__(self, values):
        super().__init__()
        self.values = sorted({v for v in values if v}, key=len, reverse=True)

    def filter(self, record):
        message = record.getMessage()
        for value in self.values:
            message = message.replace(value, "[slot]")
        record.msg, record.args = message, ()
        return True


def _quiet_dependency_logs(task: ExecutionTask) -> None:
    os.environ["BROWSER_USE_LOGGING_LEVEL"] = "warning"
    redact = _RedactSlots([s.value for s in task.slots])
    for name in ("browser_use", "cdp_use", "bubus", ""):
        logger = logging.getLogger(name)
        logger.addFilter(redact)
        for handler in logger.handlers:
            handler.addFilter(redact)


class FixtureBrowser:
    """Filters SOH candidates and revalidates fixture node identity before dispatch."""

    def __init__(self, task: ExecutionTask, *, chrome: str | None = None, faults: dict | None = None):
        _quiet_dependency_logs(task)
        from systemone_harness.envs.browser import BrowserEnvironment

        self.task = task
        self.faults = dict(faults or {})  # Acceptance fault injection only; see runtime/faults.py.
        self.gate = None
        self._replaced = False
        self.session_id = uuid.uuid4().hex
        # A fresh, empty profile per attempt; never an attached or reused browser.
        self.profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
        self.fresh_profile = False
        self.backend = BrowserEnvironment(headless=True, executable_path=chrome,
                                          start_url=task.origin + task.start_path,
                                          text_values={s.reference: s.value for s in task.slots},
                                          user_data_dir=self.profile)
        self.targets: dict[str, tuple] = {}
        self.content_gaps: list[str] = []
        self.filled_targets: list[str] = []
        self.page: tuple | None = None
        self.observation_url = ""
        self.unsupported = False

    @staticmethod
    def action_space():
        from systemone_harness.envs.browser import BrowserEnvironment
        space = BrowserEnvironment.action_space()
        # No focus-dependent or unconstrained actions on the controlled fixture.
        for name in ("select_option", "press_key", "hold_key", "scroll", "go_back", "switch_tab", "wait"):
            space.actions.pop(name, None)
        return space

    def permitted(self, operation: str) -> bool:
        """Replaced by the live gate policy when run under ControlledEnvironment."""
        return operation in self.task.permissions

    def session_info(self) -> dict:
        return {"session_id": self.session_id, "fresh_profile": self.fresh_profile,
                "attached": bool(getattr(self.backend, "cdp_url", None))}

    def reset(self, goal):
        profile = getattr(self, "profile", None)
        self.fresh_profile = bool(profile) and os.path.isdir(profile) and not os.listdir(profile)
        self.backend.reset(goal)
        _quiet_dependency_logs(self.task)  # Browser Use installs its handlers on first start.

    # ── isolated dependency access (Browser Use 0.13.10 / CDP) ──
    def _identity(self) -> dict:
        async def read():
            cdp = await self.backend._session.get_or_create_cdp_session()
            tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
            frame = tree["frameTree"]["frame"]
            return {"target_id": str(cdp.target_id), "frame_id": str(frame.get("id")),
                    "document_id": str(frame.get("loaderId")), "url": str(frame.get("url", "")),
                    "child_frames": len(tree["frameTree"].get("childFrames") or [])}
        return self.backend._run(read())

    def _properties(self, backend_ids) -> dict:
        async def read():
            cdp = await self.backend._session.get_or_create_cdp_session()
            send, out = cdp.cdp_client.send, {}
            for backend_id in backend_ids:
                try:
                    node = await send.DOM.resolveNode(params={"backendNodeId": int(backend_id)},
                                                      session_id=cdp.session_id)
                    object_id = (node.get("object") or {}).get("objectId")
                    if not object_id:
                        continue
                    value = await send.Runtime.callFunctionOn(
                        params={"objectId": object_id, "functionDeclaration": _NODE_FACTS,
                                "returnByValue": True}, session_id=cdp.session_id)
                    facts = (value.get("result") or {}).get("value")
                    if type(facts) is dict:
                        out[int(backend_id)] = facts
                except Exception:  # noqa: BLE001 - a node that cannot be resolved is not admissible
                    continue
            return out
        return self.backend._run(read())

    def _replace_node(self, backend_id: int) -> None:
        """Fault injection: swap a node for an identical clone carrying the same fixture ID."""
        async def replace():
            cdp = await self.backend._session.get_or_create_cdp_session()
            node = await cdp.cdp_client.send.DOM.resolveNode(params={"backendNodeId": int(backend_id)},
                                                             session_id=cdp.session_id)
            await cdp.cdp_client.send.Runtime.callFunctionOn(
                params={"objectId": node["object"]["objectId"],
                        "functionDeclaration": "function () { this.replaceWith(this.cloneNode(true)); }"},
                session_id=cdp.session_id)
        self.backend._run(replace())

    def _selector_map(self) -> dict:
        return self.backend._run(self.backend._session.get_selector_map())

    # ── identity and admissibility ──
    def _map(self) -> dict[str, tuple]:
        nodes = self._selector_map()
        page = self.page or {}
        self.content_gaps = []  # Enabled required text fields that are empty in this observation.
        self.filled_targets = []  # Text fields holding a value in this observation (never the value).
        result, seen_ids, candidates = {}, set(), {}
        for index, node in nodes.items():
            attrs = getattr(node, "attributes", None)
            if attrs is None:
                self.unsupported = True  # Attributes are required to establish target identity.
                continue
            node_target = getattr(node, "target_id", None)
            if node_target and page.get("target_id") and str(node_target) != page["target_id"]:
                self.unsupported = True  # Cross-target (out-of-process frame) nodes are unsupported.
                continue
            if getattr(node, "frame_id", None) not in (None, page.get("frame_id")):
                self.unsupported = True
                continue
            target_id = attrs.get("data-reflex-id")
            if target_id not in OPERATIONS:
                continue
            if target_id in seen_ids:
                self.unsupported = True
                continue
            seen_ids.add(target_id)
            backend_id = getattr(node, "backend_node_id", None)
            if not backend_id:
                self.unsupported = True
                continue
            candidates[str(index)] = (int(backend_id), str(target_id))
        facts = self._properties([backend_id for backend_id, _ in candidates.values()]) if candidates else {}
        for index, (backend_id, target_id) in candidates.items():
            live = facts.get(backend_id)
            if live is None or live.get("reflex_id") != target_id or not live.get("connected"):
                continue  # Not resolvable now: not offered, and stale if it was proposed.
            if live.get("duplicates") != 1 or not live.get("top_frame"):
                self.unsupported = True
                continue
            result[index] = (backend_id, target_id, str(live.get("tag") or ""), str(live.get("type") or ""),
                             live.get("href"), bool(live.get("disabled")), live.get("form_action"),
                             live.get("form_method"), page.get("document_id"))
            if OPERATIONS[target_id] == "type_text" and live.get("empty") is False:
                self.filled_targets.append(target_id)
            if (OPERATIONS[target_id] == "type_text" and live.get("required") and live.get("empty") is True
                    and not live.get("disabled")):
                self.content_gaps.append(target_id)
        return result

    def _trusted(self, target: tuple) -> bool:
        _, target_id, tag, kind, href, _, form_action, form_method, _ = target
        if target_id in LINKS:
            return tag == "a" and href == self.task.origin + LINKS[target_id] and self._valid_url(href)
        expected = CONTROLS.get(target_id)
        return (expected is not None and (tag, kind) == expected[:2] and
                form_action == self.task.origin + expected[2] and form_method == expected[3])

    def _admissible(self, target: tuple) -> bool:
        return (self.permitted(OPERATIONS[target[1]]) and not target[5] and self._trusted(target))

    def observe(self):
        obs = self.backend.observe()
        self.unsupported = False
        self.page = self._identity()
        if self.page["child_frames"]:
            raise RuntimeStop("adapter_contract_unsupported")  # Frames are outside the V0.5 contract.
        self.observation_url = self.page["url"]
        # Both the enumerated state and the live frame must be fixture pages; node facts below
        # exclude any enumerated node that does not belong to the live document.
        if not self._valid_url(self.observation_url) or not self._valid_url(str(obs.fields.get("url", ""))):
            raise ValueError("fixture_url_outside_origin")
        self.targets = self._map()
        if self.unsupported:
            raise RuntimeStop("adapter_contract_unsupported")
        permitted = {index: OPERATIONS[node[1]] for index, node in self.targets.items() if self._admissible(node)}
        obs.candidates["elements"] = {index: f"{label} [reflex:{self.targets[index][1]}]" for index, label in
                                      obs.candidates.get("elements", {}).items()
                                      if index in permitted and permitted[index] != "type_text"}
        obs.candidates["text_fields"] = {index: f"{label} [reflex:{self.targets[index][1]}]" for index, label in
                                         obs.candidates.get("text_fields", {}).items()
                                         if index in permitted and permitted[index] == "type_text"}
        if not obs.candidates.get("text_fields"):
            obs.candidates.pop("text_fields", None)
            obs.candidates.pop("text_values", None)
        else:
            obs.candidates["text_values"] = {s.reference: s.reference for s in self.task.slots}
        for name in ("options", "tabs"):
            obs.candidates.pop(name, None)
        obs.fields["run_id"] = self.task.run_id
        obs.fields["document_id"] = self.page["document_id"]
        obs.fields["target_ids"] = [node[1] for node in self.targets.values()]
        obs.fields["content_gaps"] = sorted(self.content_gaps)
        obs.fields["filled_targets"] = sorted(self.filled_targets)
        return obs

    def _valid_url(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (parsed.scheme == "http" and parsed.netloc == f"127.0.0.1:{urlsplit(self.task.origin).port}"
                    and parsed.path in PATHS and not parsed.query and not parsed.fragment)
        except (TypeError, ValueError):
            return False

    def admit(self, action: str, params: dict) -> str | None:
        if action == "navigate" and params == {"bootstrap": True}:
            return (None if self.permitted("navigate") and self.task.start_path in PATHS
                    else "policy_denied")
        if self.unsupported:
            return "adapter_contract_unsupported"
        if action not in ("click", "type_text") or type(params) is not dict:
            return "invalid_action"
        expected_keys = {"element"} if action == "click" else {"field", "value"}
        if set(params) != expected_keys:
            return "invalid_action"
        key = "element" if action == "click" else "field"
        if type(params[key]) is not str or not params[key].isdecimal():
            return "invalid_action"
        index = params[key]
        before = self.targets.get(index)
        if before is None or before[5]:
            return "invalid_action"  # Unknown, unresolvable or disabled targets were never offered.
        operation = OPERATIONS[before[1]]
        if not self.permitted(operation) or (action == "type_text") != (operation == "type_text"):
            return "policy_denied"
        if not self._trusted(before):
            return "policy_denied"  # Destination or form endpoint differs from the fixture definition.
        if action == "type_text" and (type(params["value"]) is not str or
                                      params["value"] not in {s.reference for s in self.task.slots}):
            return "invalid_action"
        if not self._valid_url(self.observation_url):
            return "policy_denied"
        if getattr(self, "faults", {}).get("replace_before_admit") == before[1] and not self._replaced:
            self._replaced = True
            self._replace_node(before[0])
        # Revalidation: same document, same connected node, same relevant attributes.
        page = self._identity()
        if (page["child_frames"] or not self._valid_url(page["url"]) or self.page is None or
                (page["target_id"], page["frame_id"], page["document_id"]) !=
                (self.page["target_id"], self.page["frame_id"], self.page["document_id"])):
            return "stale_target"
        live = self._properties([before[0]]).get(before[0])
        if (live is None or not live.get("connected") or live.get("duplicates") != 1 or
                not live.get("top_frame")):
            return "stale_target"
        now = (before[0], live.get("reflex_id"), str(live.get("tag") or ""), str(live.get("type") or ""),
               live.get("href"), bool(live.get("disabled")), live.get("form_action"),
               live.get("form_method"), page["document_id"])
        if now != before:
            return "stale_target"
        return None

    def describe(self, action: str, params: dict) -> dict:
        if action == "navigate":
            return {"operation": "navigate", "target_id": "bootstrap"}
        index = str(params.get("element" if action == "click" else "field", ""))
        target = self.targets.get(index)
        if target is None:
            return {"operation": "unknown"}
        info = {"operation": OPERATIONS[target[1]], "target_id": target[1]}
        if info["operation"] == "navigate":
            info["destination"] = urljoin(self.observation_url, target[4] or "")
        if action == "type_text":
            value = params.get("value")
            # Only a declared reference is ever recorded; anything else is a substitution.
            info["slot_ref"] = value if value in {s.reference for s in self.task.slots} else "invalid"
        return info

    def execute(self, action, params):
        faults = getattr(self, "faults", {})
        target = self.targets.get(str(params.get("element" if action == "click" else "field", "")))
        target_id = target[1] if target else None
        result = self.backend.execute(action, params)
        if target_id is None:
            return result
        if faults.get("hang_driver_on") == target_id:
            sleep_forever()  # The driver acted but never returns.
        if faults.get("lose_return_on") == target_id:
            from systemone_harness.environment import Result
            return Result(ok=False, text="driver return lost (injected)")
        hold = faults.get("hold_driver")
        if hold and hold["target_id"] == target_id:
            if hold["until"] == "deadline" and getattr(self, "gate", None) is not None:
                time.sleep(max(0.0, self.gate.deadline + 1.0 - time.monotonic()))
            elif hold["until"] != "deadline":
                time.sleep(hold["until"])
        return result

    def close(self):
        if getattr(self, "faults", {}).get("hang_close"):
            sleep_forever()
        try:
            self.backend.close()
        finally:
            profile = getattr(self, "profile", None)
            if profile and os.path.basename(profile).startswith(PROFILE_PREFIX):
                shutil.rmtree(profile, ignore_errors=True)
