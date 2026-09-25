"""Narrow Browser Use adapter for the isolated V0.5 fixture, loaded only when installed."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import RuntimeStop

OPERATIONS = {
    "home": "navigate", "profile": "navigate", "settings": "navigate",
    "reports": "navigate", "form": "navigate", "danger": "navigate", "export": "navigate",
    "notify-email": "toggle_setting", "save-settings": "save_settings",
    "name": "type_text", "email": "type_text", "send-form": "submit_form",
    "start-export": "start_export", "delete-account": "delete_account",
}
PATHS = {"/", "/profile", "/reports", "/settings", "/form", "/danger", "/slow"}


class FixtureBrowser:
    """Filters SOH candidates and revalidates fixture node identity before dispatch.

    BrowserEnvironment currently retains the selector map inside the Browser Use session.
    This dependency-specific access is isolated here and must be confirmed by a B run.
    """

    def __init__(self, task: ExecutionTask, *, chrome: str | None = None):
        from systemone_harness.envs.browser import BrowserEnvironment

        self.task = task
        self.backend = BrowserEnvironment(headless=True, executable_path=chrome,
                                          start_url=task.origin + task.start_path,
                                          text_values={s.reference: s.value for s in task.slots},
                                          user_data_dir=None)
        self.targets: dict[str, tuple] = {}
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

    def reset(self, goal):
        self.backend.reset(goal)

    def _map(self) -> dict[str, tuple]:
        nodes = self.backend._run(self.backend._session.get_selector_map())
        result = {}
        seen_ids = set()
        for index, node in nodes.items():
            attrs = getattr(node, "attributes", None) or {}
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
            result[str(index)] = (int(backend_id), str(target_id), str(node.tag_name or "").lower(),
                                  str(attrs.get("type") or ""), str(attrs.get("href") or ""))
        return result

    def observe(self):
        obs = self.backend.observe()
        self.observation_url = str(obs.fields.get("url", ""))
        self.unsupported = False
        if not self._valid_url(self.observation_url):
            raise ValueError("fixture_url_outside_origin")
        self.targets = self._map()
        if self.unsupported:
            raise RuntimeStop("adapter_contract_unsupported")
        permitted = {index: OPERATIONS[node[1]] for index, node in self.targets.items()
                     if OPERATIONS[node[1]] in self.task.permissions}
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
        obs.fields["target_ids"] = [node[1] for node in self.targets.values()]
        return obs

    def _valid_url(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (parsed.scheme == "http" and parsed.netloc == f"127.0.0.1:{urlsplit(self.task.origin).port}"
                    and parsed.path in PATHS and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def admit(self, action: str, params: dict) -> str | None:
        if action == "navigate" and params == {"bootstrap": True}:
            return None if "navigate" in self.task.permissions and self.task.start_path in PATHS else "policy_denied"
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
        if before is None:
            return "invalid_action"
        operation = OPERATIONS[before[1]]
        if operation not in self.task.permissions or (action == "type_text") != (operation == "type_text"):
            return "policy_denied"
        if operation == "navigate" and not self._valid_url(urljoin(self.observation_url, before[4])):
            return "policy_denied"
        if action == "type_text" and (type(params["value"]) is not str or
                                      params["value"] not in {s.reference for s in self.task.slots}):
            return "invalid_action"
        if not self._valid_url(self.observation_url):
            return "policy_denied"
        live = self.backend.observe()
        if not self._valid_url(str(live.fields.get("url", ""))):
            return "stale_target"
        now = self._map().get(index)
        if self.unsupported:
            return "adapter_contract_unsupported"
        if now != before:
            return "stale_target"
        return None

    def describe(self, action: str, params: dict) -> dict:
        if action == "navigate":
            return {"operation": "navigate", "target_id": self.task.start_path}
        index = str(params.get("element" if action == "click" else "field", ""))
        target = self.targets.get(index)
        if target is None:
            return {"operation": "unknown"}
        info = {"operation": OPERATIONS[target[1]], "target_id": target[1]}
        if info["operation"] == "navigate":
            info["destination"] = urljoin(self.observation_url, target[4])
        if action == "type_text":
            info["slot_ref"] = str(params.get("value"))
        return info

    def execute(self, action, params):
        return self.backend.execute(action, params)

    def close(self):
        self.backend.close()
