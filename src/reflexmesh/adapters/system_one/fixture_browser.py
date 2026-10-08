"""Narrow Browser Use adapter for the isolated V0.5 fixture, loaded only when installed."""

from __future__ import annotations

import inspect
import logging
import os
import stat
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import RuntimeStop
from reflexmesh.text.slots import PreparedAction, PreparedTextCommand, SlotRegistry

OPERATIONS = {
    "home": "navigate", "profile": "navigate", "settings": "navigate",
    "reports": "navigate", "form": "navigate", "danger": "navigate", "export": "navigate",
    "notify-email": "toggle_setting", "save-settings": "save_settings",
    "name": "type_text", "email": "type_text", "send-form": "submit_form",
    "start-export": "start_export", "delete-account": "delete_account",
}
PATHS = {"/", "/profile", "/reports", "/settings", "/form", "/danger", "/slow"}
CLAIM_FIELDS = frozenset({"task_id", "task_revision", "attempt_id", "run_id", "claim_id",
                          "instance_id", "owner_epoch"})
SESSION_FIELDS = CLAIM_FIELDS | {"session_id", "profile_id"}


class FixtureBrowser:
    """Filters SOH candidates and revalidates fixture node identity before dispatch.

    BrowserEnvironment currently retains the selector map inside the Browser Use session.
    This dependency-specific access is isolated here and must be confirmed by a B run.
    """

    def __init__(self, task: ExecutionTask, *, chrome: str | None = None):
        self.task = task
        self._chrome = chrome
        # SOH creates an event-loop thread in its constructor. Defer both that
        # work and the private profile until reset, after harness setup succeeds.
        self.backend = None
        self._fixture_owner = None
        self._claim_snapshot = None
        self._bound_identity = None
        self._owned_session = self._owned_profile = None
        self._profile_path = self._profile_stat = None
        self._profile_id = None
        self._session_cdp_url = None
        self._owner_cdp_client = None
        self._cookie_ready = False
        self._ownership_failure = None
        self._closed = False
        self.targets: dict[str, tuple] = {}
        self.observation_url = ""
        self.unsupported = False
        self._slot_registry = None
        self._slot_sink = None

    def _ownership_stop(self, reason=None):
        reason = reason or ("ownership_lost" if getattr(self, "_cookie_ready", False)
                            else "ownership_unavailable")
        self._ownership_failure = reason
        return RuntimeStop(reason)

    def _ownership_changed(self, reason, *, session_id=None):
        """Persist a concrete post-bind mismatch before worker-local unwinding.

        A missing field, a transport error or an unsupported SDK never calls
        this producer. Absolute paths, endpoints and credentials stay private.
        """
        if getattr(self, "_cookie_ready", False):
            try:
                self._fixture_owner._record_browser_violation(
                    reason, session_id=session_id, profile_id=self._profile_id)
            except Exception:
                # Failed evidence capture must still stop dispatch, but cannot
                # manufacture a trusted negative assessment.
                pass
        return self._ownership_stop()

    def _claim_binding(self) -> dict:
        from reflexmesh.runtime.ownership import FixtureOwnership

        owner = getattr(self, "_fixture_owner", None)
        if type(owner) is not FixtureOwnership or owner.is_active is not True:
            raise self._ownership_stop()
        binding = owner.binding
        if (type(binding) is not dict or set(binding) not in (CLAIM_FIELDS, SESSION_FIELDS) or
                any(type(binding[key]) is not str or not binding[key] or len(binding[key]) > 128
                    for key in CLAIM_FIELDS - {"task_revision", "owner_epoch"}) or
                any(type(binding[key]) is not int or binding[key] <= 0
                    for key in ("task_revision", "owner_epoch")) or
                (binding["task_id"], binding["task_revision"], binding["run_id"]) !=
                (self.task.task_id, self.task.revision, self.task.run_id)):
            raise self._ownership_stop()
        claim = {key: binding[key] for key in CLAIM_FIELDS}
        if self._claim_snapshot is not None and claim != self._claim_snapshot:
            raise self._ownership_stop()
        return dict(binding)

    def bind_ownership(self, owner) -> None:
        """Enroll the acquired capability without allocating browser resources.

        This binds fixture effects to one attempt/session, not to an individual
        action or an opaque internal retry. Other OS/controller access remains
        outside this narrow contract.
        """
        if getattr(self, "_ownership_failure", None) or getattr(self, "_closed", False):
            raise self._ownership_stop()
        current = getattr(self, "_fixture_owner", None)
        if current is not None:
            if current is not owner:
                raise self._ownership_stop()
            self._assert_ownership(allow_unstarted=True)
            return
        self._fixture_owner = owner
        try:
            binding = self._claim_binding()
            if set(binding) != CLAIM_FIELDS:
                raise self._ownership_stop()
            self._claim_snapshot = dict(binding)
            self._assert_ownership(allow_unstarted=True)
        except RuntimeStop:
            raise
        except Exception:
            raise self._ownership_stop() from None

    def _check_profile(self, path) -> None:
        if not isinstance(path, (str, Path)) or not path:
            raise self._ownership_stop()
        actual = Path(path)
        private = self._profile_path
        if private is None:
            raise self._ownership_stop()
        if (actual.is_symlink() or private.is_symlink() or
                actual.resolve(strict=True) != private):
            raise self._ownership_changed("browser_profile_changed")
        info = private.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or
                info.st_uid != os.geteuid() or (info.st_dev, info.st_ino) != self._profile_stat):
            raise self._ownership_changed("browser_profile_changed")

    def _session_identity(self):
        if (self.backend.cdp_url is not None or
                self.backend.start_url != self.task.origin + self.task.start_path):
            raise self._ownership_changed("browser_configuration_changed")
        self._check_profile(self.backend.user_data_dir)
        session = self.backend._session
        if session is None:
            raise self._ownership_stop()
        session_id = session.id
        profile = session.browser_profile
        if type(session_id) is not str or not session_id or len(session_id) > 128:
            raise self._ownership_stop()
        if profile.is_local is False or profile.use_cloud is True:
            raise self._ownership_changed("browser_configuration_changed")
        if profile.is_local is not True or profile.use_cloud is not False:
            raise self._ownership_stop()
        self._check_profile(profile.user_data_dir)
        # A locally launched Browser Use session gets its own CDP URL at start.
        # The SOH constructor's cdp_url must remain None; capture, then pin the
        # resulting connection rather than forbidding that normal local URL.
        cdp_url = session.cdp_url
        if type(cdp_url) is not str or not cdp_url:
            raise self._ownership_stop()
        return session, profile, session_id, cdp_url

    def _assert_ownership(self, *, allow_unstarted=False) -> dict | None:
        """Recheck actual backend state; no caller-supplied ownership assertion."""
        try:
            if getattr(self, "_ownership_failure", None) or getattr(self, "_closed", False):
                raise self._ownership_stop()
            binding = self._claim_binding()
            if self.backend is None:
                if (allow_unstarted and self._bound_identity is None and
                        self._profile_path is None and not self._cookie_ready and
                        set(binding) == CLAIM_FIELDS):
                    return None
                raise self._ownership_stop()
            if (self.backend.cdp_url is not None or
                    self.backend.start_url != self.task.origin + self.task.start_path):
                raise self._ownership_changed("browser_configuration_changed")
            self._check_profile(self.backend.user_data_dir)
            if self._bound_identity is None:
                if (not allow_unstarted or self.backend._session is not None or
                        set(binding) != CLAIM_FIELDS):
                    raise self._ownership_stop()
                return None
            session, profile, session_id, cdp_url = self._session_identity()
            if (session is not self._owned_session or
                    session_id != self._bound_identity["session_id"]):
                raise self._ownership_changed("browser_session_changed", session_id=session_id)
            if profile is not self._owned_profile:
                raise self._ownership_changed("browser_profile_changed", session_id=session_id)
            if cdp_url != self._session_cdp_url:
                raise self._ownership_changed("browser_configuration_changed", session_id=session_id)
            if (binding != self._bound_identity or
                    (not self._cookie_ready and not allow_unstarted)):
                raise self._ownership_stop()
            self._fixture_owner.assert_session(session_id, self._profile_id)
            if self._cookie_ready:
                self._check_private_transport(self._owner_cdp_client)
            return dict(self._bound_identity)
        except RuntimeStop:
            raise
        except Exception:
            raise self._ownership_stop() from None

    def _check_private_transport(self, client):
        # websockets DEBUG can log the entire CDP frame, including cookies in
        # network events. Check both cached transport state and current logging.
        if (getattr(client.ws, "debug", None) is not False or
                any(logging.getLogger(name).isEnabledFor(logging.DEBUG)
                    for name in ("cdp_use.client", "websockets.client", "websockets.protocol"))):
            raise self._ownership_stop()

    async def _install_owner_cookie(self):
        """Pinned CDP handoff, kept outside observations, model input and trace."""
        self._assert_ownership(allow_unstarted=True)
        session = self._owned_session
        get_cdp = session.get_or_create_cdp_session
        if not inspect.iscoroutinefunction(inspect.unwrap(get_cdp)):
            raise self._ownership_stop()
        cdp = await get_cdp()
        self._assert_ownership(allow_unstarted=True)
        cdp_session_id = cdp.session_id
        network = cdp.cdp_client.send.Network
        set_cookie, get_cookies = network.setCookie, network.getCookies
        if type(cdp_session_id) is not str or not cdp_session_id:
            raise self._ownership_stop()
        for method in (set_cookie, get_cookies):
            if not inspect.iscoroutinefunction(inspect.unwrap(method)):
                raise self._ownership_stop()
            inspect.signature(method).bind(params={}, session_id=cdp_session_id)
        self._check_private_transport(cdp.cdp_client)
        self._owner_cdp_client = cdp.cdp_client
        secret = self._fixture_owner.browser_secret
        if type(secret) is not str or not secret:
            raise self._ownership_stop()
        response = await set_cookie(params={
            "name": "ReflexMeshOwner", "value": secret, "url": self.task.origin,
            "path": "/", "httpOnly": True, "sameSite": "Strict",
            # Deliberately no expires: this is a session cookie.
        }, session_id=cdp_session_id)
        if type(response) is not dict or response.get("success") is not True:
            raise self._ownership_stop()
        readback = await get_cookies(params={"urls": [self.task.origin + "/"]},
                                     session_id=cdp_session_id)
        if type(readback) is not dict or type(readback.get("cookies")) is not list:
            raise self._ownership_stop()
        matches = [cookie for cookie in readback["cookies"]
                   if type(cookie) is dict and cookie.get("name") == "ReflexMeshOwner"]
        if (len(matches) != 1 or matches[0].get("value") != secret or
                matches[0].get("domain") != "127.0.0.1" or matches[0].get("path") != "/" or
                matches[0].get("httpOnly") is not True or matches[0].get("sameSite") != "Strict" or
                matches[0].get("session") is not True):
            raise self._ownership_stop()
        self._assert_ownership(allow_unstarted=True)

    def bind_runtime(self, registry: SlotRegistry, sink) -> None:
        """Accept only the supervisor's snapshot for this exact task revision/run."""
        if (type(registry) is not SlotRegistry or
                (registry.task_id, registry.revision, registry.run_id) !=
                (self.task.task_id, self.task.revision, self.task.run_id) or
                not callable(getattr(sink, "handoff", None))):
            raise RuntimeStop("adapter_contract_unsupported")
        current = getattr(self, "_slot_registry", None)
        if current is not None and (current is not registry or self._slot_sink is not sink):
            raise RuntimeStop("adapter_contract_unsupported")
        self._slot_registry, self._slot_sink = registry, sink

    @staticmethod
    def action_space():
        from systemone_harness.envs.browser import BrowserEnvironment
        space = BrowserEnvironment.action_space()
        # No focus-dependent or unconstrained actions on the controlled fixture.
        for name in ("select_option", "press_key", "hold_key", "scroll", "go_back", "switch_tab", "wait"):
            space.actions.pop(name, None)
        return space

    def reset(self, goal):
        self._assert_ownership(allow_unstarted=True)
        if self._bound_identity is None:
            try:
                if self.backend is None:
                    from systemone_harness.envs.browser import BrowserEnvironment

                    # Allocate only when bootstrap actually starts. Provider,
                    # action-space and controller setup failures leave no temp
                    # profile or SOH event-loop thread to clean up.
                    # Browser Use 5c892e01 recognizes this private temp prefix
                    # and skips its automatic Chrome-profile copy path.
                    self._profile_path = Path(tempfile.mkdtemp(
                        prefix="browser-use-user-data-dir-reflexmesh-")).resolve(strict=True)
                    info = self._profile_path.lstat()
                    self._profile_stat = (info.st_dev, info.st_ino)
                    self._profile_id = uuid.uuid4().hex  # Public identity, never a credential.
                    self.backend = BrowserEnvironment(
                        headless=True, executable_path=self._chrome, cdp_url=None,
                        start_url=self.task.origin + self.task.start_path,
                        # These remain labels; immutable payloads use the slot boundary.
                        text_values={s.reference: s.reference for s in self.task.slots},
                        user_data_dir=str(self._profile_path))
                    self._assert_ownership(allow_unstarted=True)
                if (not callable(self.backend._run) or
                        not inspect.iscoroutinefunction(inspect.unwrap(self.backend._start))):
                    raise self._ownership_stop()
                # SOH ab8e8f08 _start starts a blank session. Its reset is the
                # first fixture navigation and must happen only AFTER binding.
                self.backend._run(self.backend._start())
                session, profile, session_id, cdp_url = self._session_identity()
                expected = {**self._claim_snapshot, "session_id": session_id,
                            "profile_id": self._profile_id}
                receipt = self._fixture_owner.bind_session(session_id, self._profile_id, timeout=2.0)
                if type(receipt) is not dict or receipt.get("binding") != expected:
                    raise self._ownership_stop()
                self._bound_identity = dict(expected)
                self._owned_session, self._owned_profile = session, profile
                self._session_cdp_url = cdp_url
                self.backend._run(self._install_owner_cookie())
                self._cookie_ready = True
            except RuntimeStop:
                raise
            except Exception:
                # CDP validation/transport errors may contain cookie values.
                raise self._ownership_stop() from None
        self._assert_ownership()
        self.backend.reset(goal)
        self._assert_ownership()

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
        self._assert_ownership()
        obs = self.backend.observe()
        self._assert_ownership()
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
            registry = getattr(self, "_slot_registry", None)
            references = registry.references if registry is not None else tuple(s.reference for s in self.task.slots)
            obs.candidates["text_values"] = {ref: ref for ref in references}
        for name in ("options", "tabs"):
            obs.candidates.pop(name, None)
        obs.fields["run_id"] = self.task.run_id
        obs.fields["target_ids"] = [node[1] for node in self.targets.values()]
        obs.fields["ownership"] = self._assert_ownership()
        return obs

    def _valid_url(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (parsed.scheme == "http" and parsed.netloc == f"127.0.0.1:{urlsplit(self.task.origin).port}"
                    and parsed.path in PATHS and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def admit(self, action: str, params: dict) -> str | None:
        self._assert_ownership(allow_unstarted=action == "navigate" and params == {"bootstrap": True})
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
        registry = getattr(self, "_slot_registry", None)
        references = registry.references if registry is not None else tuple(s.reference for s in self.task.slots)
        if action == "type_text" and (type(params["value"]) is not str or params["value"] not in references):
            return "invalid_action"
        if not self._valid_url(self.observation_url):
            return "policy_denied"
        live = self.backend.observe()
        self._assert_ownership()
        if not self._valid_url(str(live.fields.get("url", ""))):
            return "stale_target"
        now = self._map().get(index)
        self._assert_ownership()
        if self.unsupported:
            return "adapter_contract_unsupported"
        if now != before:
            return "stale_target"
        return None

    def prepare(self, action: str, params: dict) -> PreparedAction:
        """Snapshot before admission; resolve exactly once after it succeeds."""
        if type(action) is not str or type(params) is not dict:
            raise RuntimeStop("invalid_action")
        captured = dict(params)
        keys = {"element"} if action == "click" else {"field", "value"}
        if (action not in ("click", "type_text") or set(captured) != keys or
                any(type(value) is not str for value in captured.values())):
            raise RuntimeStop("invalid_action")
        index = captured["element" if action == "click" else "field"]
        target = self.targets.get(index)
        parameters = tuple(captured.items())
        reason = self.admit(action, dict(parameters))
        if reason:
            raise RuntimeStop(reason)
        # The admitted node tuple and immutable parameters supply the descriptor
        # and the driver, even if callers mutate their dictionaries afterwards.
        if target is None:
            raise RuntimeStop("invalid_action")
        text = None
        if action == "type_text":
            registry = getattr(self, "_slot_registry", None)
            if registry is None or getattr(self, "_slot_sink", None) is None:
                raise RuntimeStop("adapter_contract_unsupported")
            try:
                text = PreparedTextCommand(int(index), registry.resolve(captured["value"]))
            except ValueError:
                raise RuntimeStop("invalid_action") from None
        operation = OPERATIONS[target[1]]
        destination = urljoin(self.observation_url, target[4]) if operation == "navigate" else None
        return PreparedAction(action, operation, target[1], parameters, destination, text)

    def execute_prepared(self, prepared: PreparedAction, action_id: int):
        self._assert_ownership()
        if type(prepared) is not PreparedAction:
            raise RuntimeStop("adapter_contract_unsupported")
        if prepared.action == "type_text":
            from reflexmesh.adapters.system_one.text_dispatch import dispatch_text

            if prepared._text is None or getattr(self, "_slot_sink", None) is None:
                raise RuntimeStop("adapter_contract_unsupported")
            result = dispatch_text(self.backend, prepared._text, action_id, self._slot_sink)
            self._assert_ownership()
            return result
        if prepared.action != "click" or prepared._text is not None:
            raise RuntimeStop("adapter_contract_unsupported")
        result = self.backend.execute(prepared.action, dict(prepared._parameters))
        self._assert_ownership()
        return result

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
        self._assert_ownership()
        # The legacy mutable backend path must never be usable for text, even
        # outside the supervised prepared-command protocol.
        if action == "type_text":
            raise RuntimeStop("adapter_contract_unsupported")
        result = self.backend.execute(action, params)
        self._assert_ownership()
        return result

    def close(self):
        self._closed = True
        try:
            if self.backend is not None:
                self.backend.close()
        except Exception:
            raise self._ownership_stop("ownership_lost") from None
        # Pinned SOH close is best-effort and can swallow kill errors. Do not
        # release/revoke ownership or delete a potentially live profile here.
        # The supervisor fences effects and confirms process-group cleanup.
