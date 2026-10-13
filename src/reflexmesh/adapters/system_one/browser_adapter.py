"""Generic Browser Use adapter for sites with no prepared scenario (V0.6, M1).

It sits on SystemOneHarness's `BrowserEnvironment` (text observation, indexed controls,
`Tools.act`) and adds what the runtime needs to treat a page it has never seen as an
admission problem: every offered option is bound to the DOM node, frame and document it came
from, and is re-checked against a fresh capture immediately before it is dispatched.

Nothing here knows a site. There is no `data-reflex-id`, no path whitelist and no scenario:
controls come from the page's own accessibility tree.

Boundaries this module does NOT claim: Browser Use's internal behavior inside one
`Tools.act` (it may retry), focus, drag and drop, closed shadow DOM, canvas, or what a server
did with a request. Those stay `unknown` in the result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from reflexmesh.contracts.browser import BrowserTask, READ_OPERATIONS, canonical_origin
from reflexmesh.runtime.runner import RuntimeStop
from reflexmesh.text.slots import PreparedTextCommand, SlotRegistry

BLANK_URLS = ("", "about:blank")
BLANK_RETRIES, BLANK_WAIT = 6, 0.5
PROFILE_PREFIX = "browser-use-user-data-dir-reflexmesh-"
MAX_ELEMENTS = 200
MAX_TEXT_CHARS = 8000
MAX_NAME = 80
TEXT_INPUT_TYPES_EXCLUDED = {"checkbox", "radio", "button", "submit", "reset", "file", "image", "range", "color",
                             "hidden"}
KIND_OPERATION = {"elements": "click", "text_fields": "type_text", "options": "select_option",
                  "tabs": "switch_tab"}
ACTION_KIND = {"click": ("elements", "element"), "type_text": ("text_fields", "field"),
               "select_option": ("options", "option"), "switch_tab": ("tabs", "tab")}
CAPABILITIES = {
    "navigate": True, "click": True, "type_text": True, "select_option": True, "scroll": True,
    "go_back": True, "switch_tab": True, "wait": True,
    # Declared, not probed: a capability is claimed only after an integration probe shows it.
    "press_key": False, "focus_region": False,
    "frames": False, "cross_origin_frames": False, "open_shadow_dom": False,
}

ACTION_SPACE = {
    "instructions": (
        "You operate a web page toward a goal. Each step you see the page's visible text with its "
        "controls numbered, and you choose one action: click a control, type one of the supplied values "
        "into a field, pick a dropdown option, scroll, switch tab, or go back. Take controls in the "
        "order the page presents them and submit only after every field is set. Finish when the page "
        "shows the goal is done. Escalate when nothing offered can move it forward."),
    "actions": {
        "click": {"description": "Click one numbered control: a link, button, checkbox, tab or option.",
                  "risk": "write",
                  "params": {"element": {"from": "elements", "instructions": "Which control to click?"}}},
        "type_text": {"description": "Type one of the supplied values into a text field, replacing what is there.",
                      "risk": "write",
                      "params": {"field": {"from": "text_fields", "instructions": "Which field to type into?"},
                                 "value": {"from": "text_values",
                                           "instructions": "Which supplied value belongs in it?"}}},
        "select_option": {"description": "Choose an option in a dropdown.", "risk": "write",
                          "params": {"option": {"from": "options", "instructions": "Which option, in which dropdown?"}}},
        "go_back": {"description": "Go back to the previous page.", "risk": "write",
                    "params": {"to": {"from": "history", "instructions": "Back to which page?"}}},
        "scroll": {"description": "Scroll the page one screen.", "risk": "read",
                   "params": {"direction": {"choices": {"down": "further down the page", "up": "back up"},
                                            "instructions": "Which way?"}}},
        "switch_tab": {"description": "Bring another open tab to the front.", "risk": "read",
                       "params": {"tab": {"from": "tabs", "instructions": "Which tab?"}}},
        "wait": {"description": "Wait a second for the page to settle.", "risk": "read"},
    },
}


def browser_action_space(permissions) -> "ActionSpace":
    """The SOH action space limited to what the task grants; the model cannot choose what it was not shown."""
    import copy

    from systemone_harness.actions import ActionSpace

    spec = copy.deepcopy(ACTION_SPACE)
    spec["actions"] = {name: action for name, action in spec["actions"].items() if name in permissions}
    return ActionSpace.from_dict(spec)


def chrome_version(path: str) -> str | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass(frozen=True)
class Target:
    """What one offered option meant when it was offered. Frozen: admission compares, never edits."""

    kind: str
    key: str
    operation: str
    index: int | None = None
    backend_node_id: int | None = None
    target_id: str | None = None
    frame_id: str | None = None
    signature: tuple = ()
    role: str = ""
    name: str = ""
    destination: str | None = None
    option_text: str | None = None
    tab_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedBrowserAction:
    """One admission result supplies both the public descriptor and the private driver command."""

    action: str
    operation: str
    risk: str
    observation_id: int
    ref: str
    _parameters: tuple[tuple[str, str], ...] = field(repr=False)
    target: Target | None = None
    destination: str | None = None
    _text: PreparedTextCommand | None = field(default=None, repr=False)

    @property
    def slot_ref(self) -> str | None:
        return None if self._text is None else self._text.reference

    def descriptor(self) -> dict:
        info = {"operation": self.operation, "risk": self.risk, "observation_id": self.observation_id,
                "option_ref": self.ref}
        if self.target is not None:
            info["target"] = {"role": self.target.role, "name": self.target.name}
        if self.destination is not None:
            info["destination"] = self.destination
        if self.slot_ref is not None:
            info["slot_ref"] = self.slot_ref
        return info


_INDEX_MARK = re.compile(r"\*?\[\d+\]")


def essential_text(text: str) -> str:
    """Page text without Browser Use's per-capture element numbers and new-element marks."""
    lines = (" ".join(_INDEX_MARK.sub("", line).split()) for line in str(text or "").splitlines())
    return "\n".join(line for line in lines if line and line != "*")


def action_identity(prepared: "PreparedBrowserAction") -> str:
    target = prepared.target
    if target is None:
        return f"{prepared.operation}:{prepared.ref}"      # wait, scroll:up|down
    parts = [prepared.operation, target.role, target.name, target.option_text or "", target.destination or "",
             prepared.slot_ref or ""]
    return json.dumps(parts, ensure_ascii=False)


def _backend_class():
    """SOH's BrowserEnvironment with the browser profile pinned down for a controlled run."""
    from systemone_harness.envs.browser import BrowserEnvironment

    class ControlledBackend(BrowserEnvironment):
        def __init__(self, *, profile_options: dict, **kwargs):
            super().__init__(**kwargs)
            self.profile_options = dict(profile_options)

        async def _start(self):
            if self._session is not None:
                return
            from browser_use import Tools
            from browser_use.browser import BrowserProfile, BrowserSession

            profile = BrowserProfile(headless=self.headless, executable_path=self.executable_path,
                                     user_data_dir=self.user_data_dir, **self.profile_options)
            self._session = BrowserSession(browser_profile=profile)
            await self._session.start()
            self._tools = Tools()
            self._action_model = self._tools.registry.create_action_model()

    return ControlledBackend


class GenericBrowserAdapter:
    """SystemOneHarness `Environment` plus the prepared-command protocol, for any DOM site."""

    def __init__(self, task: BrowserTask, *, chrome: str, profile_dir: str | None = None,
                 sandbox: bool = True, headless: bool = True):
        self.task = task
        self.chrome, self.sandbox, self.headless = chrome, sandbox, headless
        self._own_profile = profile_dir is None
        # Browser Use 0.13.10 copies any other Chrome profile into /tmp and never removes the copy.
        # This marker in the directory name is what makes it use (and leave) the directory as given.
        self.profile_dir = profile_dir or tempfile.mkdtemp(prefix=PROFILE_PREFIX)
        os.chmod(self.profile_dir, 0o700)
        self.backend = None
        self._allowed = frozenset(task.allowed_origins)
        self._registry: SlotRegistry | None = None
        self._sink = None
        self._table: dict[tuple[str, str], Target] = {}
        self._observation_id = 0
        self._doc: dict = {}
        self.report: dict = {}
        self._pending_effect: dict | None = None
        self.effect_report: dict | None = None
        self.closed = False

    # ── binding ──
    def bind_runtime(self, registry: SlotRegistry, sink) -> None:
        if type(registry) is not SlotRegistry or not callable(getattr(sink, "handoff", None)):
            raise RuntimeStop("adapter_contract_unsupported")
        self._registry, self._sink = registry, sink

    def capabilities(self) -> dict:
        return dict(CAPABILITIES)

    # ── lifecycle ──
    def _ensure_backend(self):
        if self.backend is None:
            options = {"chromium_sandbox": self.sandbox, "enable_default_extensions": False,
                       "highlight_elements": False, "captcha_solver": False, "auto_download_pdfs": False,
                       "allowed_domains": sorted(self._allowed)}
            # The backend's own text map holds labels, never values: a value reaches the page
            # only through the prepared, witnessed text command.
            self.backend = _backend_class()(
                profile_options=options, headless=self.headless, executable_path=self.chrome, cdp_url=None,
                start_url=self.task.start_url, user_data_dir=self.profile_dir, text_chars=MAX_TEXT_CHARS,
                max_elements=MAX_ELEMENTS,
                text_values={s.reference: s.reference for s in self.task.slots})
        return self.backend

    def reset(self, goal: str) -> None:
        """First navigation. The caller has already passed it through the dispatch gate."""
        backend = self._ensure_backend()
        backend._run(backend._start())
        actual = str(backend._session.browser_profile.user_data_dir)
        if os.path.realpath(actual) != os.path.realpath(self.profile_dir):
            # Cleanup removes the directory we made; a different one in use would be left behind.
            raise RuntimeStop("adapter_contract_unsupported")
        backend.reset(goal)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.backend is not None:
                self.backend.close()
        finally:
            if self._own_profile:
                shutil.rmtree(self.profile_dir, ignore_errors=True)

    def kill(self) -> None:
        """Best-effort hard stop used by the watchdog when the driver does not answer."""
        backend = self.backend
        session = getattr(backend, "_session", None)
        if session is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(session.kill(), backend._loop).result(5.0)
        except Exception:
            pass

    # ── observation ──
    async def _capture(self):
        backend = self.backend
        obs = await backend._observe()
        session = backend._session
        state = await session.get_browser_state_summary(include_screenshot=False, cached=True)
        nodes = await session.get_selector_map()
        cdp = await session.get_or_create_cdp_session()
        tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
        frame = tree["frameTree"]["frame"]
        history = await cdp.cdp_client.send.Page.getNavigationHistory(session_id=cdp.session_id)
        entries = history.get("entries") or []
        at = history.get("currentIndex", 0)
        previous = entries[at - 1].get("url") if at and at - 1 < len(entries) else None
        doc = {"frame_id": frame.get("id"), "loader_id": frame.get("loaderId"), "target_id": cdp.target_id,
               "url": state.url, "previous_url": previous}
        return obs, state, nodes, doc

    def _origin_allowed(self, url: str) -> bool:
        return canonical_origin(url) in self._allowed

    @staticmethod
    def _signature(node) -> tuple:
        attrs = node.attributes or {}
        ax = getattr(node, "ax_node", None)
        disabled = "disabled" in attrs or str(attrs.get("aria-disabled", "")).lower() == "true"
        href = attrs.get("href") or ""
        return (str(node.tag_name or "").lower(), str(getattr(ax, "role", None) or "").lower(),
                str(getattr(ax, "name", None) or "").strip()[:MAX_NAME], str(attrs.get("type") or "").lower(),
                bool(disabled), urlsplit(href).path if href else "", bool(node.is_visible is not False))

    def _scrub(self, text):
        """Secret slot values never reach the model or a log; normal ones never reach a log."""
        if type(text) is not str:
            return text
        for slot in self.task.slots:
            if slot.sensitivity == "secret" and slot.value:
                text = text.replace(slot.value, "[secret]")
        return text

    def observe(self):
        self._ensure_backend()
        obs, state, nodes, doc = self.backend._run(self._capture())
        # A window that has just opened shows about:blank until its page arrives; that is not a
        # policy question. Give it a moment, then report it as the empty page it is.
        for _ in range(BLANK_RETRIES):
            if str(doc["url"] or "") not in BLANK_URLS:
                break
            time.sleep(BLANK_WAIT)
            obs, state, nodes, doc = self.backend._run(self._capture())
        self._doc = doc
        url = str(doc["url"] or "")
        blank = url in BLANK_URLS
        if url.startswith("chrome-error:"):
            raise RuntimeStop("navigation_failed")
        if not blank and not self._origin_allowed(url):
            raise RuntimeStop("policy_denied")
        self._observation_id += 1
        oid = self._observation_id
        granted = self.task.permissions
        main_frame = doc["frame_id"]
        usable, skipped_frames, disabled = {}, 0, 0
        for index, node in nodes.items():
            if node.frame_id and main_frame and node.frame_id != main_frame:
                skipped_frames += 1
                continue
            sig = self._signature(node)
            if not sig[6]:
                continue
            if sig[4]:
                disabled += 1
                continue
            usable[str(index)] = (node, sig)
        field_keys = set(obs.candidates.get("text_fields") or {})
        select_keys = {key.partition(":")[0] for key in (obs.candidates.get("options") or {})}
        select_keys |= {k for k, (node, _) in usable.items() if str(node.tag_name or "").lower() == "select"}
        table: dict[tuple[str, str], Target] = {}
        elements, fields, options, tabs = {}, {}, {}, {}

        def target(kind, key, node, sig, **extra):
            attrs = node.attributes or {}
            href = attrs.get("href")
            destination = urljoin(url, href) if href and kind == "elements" else None
            return Target(kind=kind, key=key, operation=KIND_OPERATION[kind], index=int(key.partition(":")[0]),
                          backend_node_id=node.backend_node_id, target_id=node.target_id, frame_id=node.frame_id,
                          signature=sig, role=sig[1] or sig[0], name=sig[2], destination=destination, **extra)

        if "click" in granted:
            for key, label in (obs.candidates.get("elements") or {}).items():
                if key in usable and key not in field_keys and key not in select_keys:
                    node, sig = usable[key]
                    entry = target("elements", key, node, sig)
                    where = urlsplit(entry.destination).path if entry.destination else ""
                    elements[key] = self._scrub(label + (f" → {where}" if where else ""))
                    table[("elements", key)] = entry
        if "type_text" in granted:
            for key, label in (obs.candidates.get("text_fields") or {}).items():
                if key in usable:
                    node, sig = usable[key]
                    fields[key] = self._scrub(label)
                    table[("text_fields", key)] = target("text_fields", key, node, sig)
        if "select_option" in granted:
            for key, label in (obs.candidates.get("options") or {}).items():
                index, _, text = key.partition(":")
                if index in usable:
                    node, sig = usable[index]
                    options[key] = self._scrub(label)
                    table[("options", key)] = target("options", key, node, sig, option_text=text)
        if "switch_tab" in granted:
            for tab in state.tabs or []:
                tab_id = str(getattr(tab, "target_id", "") or "")
                short = tab_id[-4:]
                if (short and tab_id != doc["target_id"] and self._origin_allowed(str(tab.url or ""))):
                    tabs[short] = self._scrub(str(getattr(tab, "title", "") or tab.url or short)[:MAX_NAME])
                    table[("tabs", short)] = Target(kind="tabs", key=short, operation="switch_tab",
                                                    target_id=tab_id, tab_id=tab_id, name=tabs[short])
        history = {}
        if "go_back" in granted and doc.get("previous_url") and self._origin_allowed(doc["previous_url"]):
            history = {"back": urlsplit(doc["previous_url"]).path or "/"}
            table[("history", "back")] = Target(kind="history", key="back", operation="go_back",
                                                name=history["back"], destination=doc["previous_url"])

        candidates = {}
        if elements:
            candidates["elements"] = elements
        if fields and self._registry is not None:
            candidates["text_fields"] = fields
            candidates["text_values"] = {
                slot.reference: (f"{slot.reference}: {slot.value[:200]}" if slot.sensitivity == "normal"
                                 else f"{slot.reference}: (secret value, not shown)")
                for slot in self.task.slots}
        if options:
            candidates["options"] = options
        if tabs:
            candidates["tabs"] = tabs
        if history:
            candidates["history"] = history
        self._table = table

        considered = set(elements) | set(fields) | {k.partition(":")[0] for k in options}
        unreachable = [k for k in usable if k not in considered and k not in select_keys]
        text_cut = obs.text.endswith(" …")
        # The scroll offset lives in `page_info`; the summary's own pixels_* attributes stay 0
        # (SystemOneHarness reads those and so never reports content outside the window).
        info = getattr(state, "page_info", None)
        below, above = int(getattr(info, "pixels_below", 0) or 0), int(getattr(info, "pixels_above", 0) or 0)
        reasons = []
        if unreachable and len(considered) >= MAX_ELEMENTS:
            reasons.append("control_limit")
        if text_cut:
            reasons.append("text_truncated")
        if blank:
            reasons.append("blank_page")
        if skipped_frames:
            reasons.append("frame_content_not_offered")
        if below:
            reasons.append("more_content_below")
        if above:
            reasons.append("more_content_above")
        coverage = {"status": "partial" if reasons else "complete", "reasons": reasons,
                    "controls_total": len(nodes), "controls_offered": len(considered),
                    "controls_not_offered": len(unreachable), "disabled_omitted": disabled,
                    "frame_nodes_omitted": skipped_frames,
                    "next_window": "scroll" if (below or above) else None}
        obs.candidates = candidates
        obs.text = self._scrub(obs.text)
        # No observation id here: SystemOneHarness hashes these fields to notice an action repeated on an
        # unchanged state, and a fresh id every step would make every state look new (V0.6 section 8).
        obs.fields = {"url": url, "title": self._scrub(str(state.title or "")), "controls": len(considered),
                      "tabs": len(tabs) + 1, "coverage": coverage}
        self.report = {
            "observation_id": oid, "url": url, "coverage": coverage,
            "text_chars": len(obs.text), "options": {kind: len(values) for kind, values in candidates.items()
                                                      if kind != "text_values"},
            "option_list": [{"ref": f"{oid}:{kind}:{key}", "operation": KIND_OPERATION.get(kind, "go_back"),
                             "role": entry.role, "name": entry.name}
                            for (kind, key), entry in table.items()],
            "signature": self._page_signature(obs, url)}
        if self._pending_effect is not None:
            self.effect_report = self._assess_effect(self._pending_effect, nodes)
            self._pending_effect = None
        return obs

    @staticmethod
    def _page_signature(obs, url: str) -> str:
        """The essential state (V0.6 section 8): URL, visible text and the values set so far. Browser Use
        renumbers every control on every capture and marks new ones with `*`; neither is a new state."""
        return hashlib.sha256((url + "\n" + essential_text(obs.text)).encode("utf-8", "replace")).hexdigest()[:16]

    def _assess_effect(self, pending: dict, nodes) -> dict:
        """What the page shows after the action. A UI observation, never a server effect."""
        report = {"action_id": pending["action_id"], "operation": pending["operation"], "source": "ui_observation",
                  "url_changed": self.report["url"] != pending["url"],
                  "content_changed": self.report["signature"] != pending["signature"],
                  "server_effect": "unknown"}
        if pending["operation"] == "type_text":
            node = next((n for n in nodes.values() if n.backend_node_id == pending.get("backend_node_id")), None)
            expected = self._registry.resolve(pending["slot_ref"])._value if self._registry else None
            shown = str((node.attributes or {}).get("value", "")) if node is not None else None
            report["field_present"] = node is not None
            report["field_shows_slot_value"] = (shown == expected) if shown is not None and expected is not None else None
        return report

    # ── admission ──
    def _stale(self):
        raise RuntimeStop("stale_target")

    def _find(self, nodes, entry: Target):
        for index, node in nodes.items():
            if node.backend_node_id == entry.backend_node_id and node.target_id == entry.target_id:
                return str(index), node
        return None, None

    def prepare(self, action: str, params: dict) -> PreparedBrowserAction:
        """Admit against the table of the last observation, re-check the live page, resolve once."""
        if type(action) is not str or type(params) is not dict:
            raise RuntimeStop("invalid_action")
        captured = dict(params)
        operation = action
        if operation not in self.task.permissions or operation not in CAPABILITIES or not CAPABILITIES[operation]:
            raise RuntimeStop("policy_denied")
        if any(type(v) is not str for v in captured.values()):
            raise RuntimeStop("invalid_action")
        risk = "read" if operation in READ_OPERATIONS else "write"
        oid = self._observation_id
        if operation == "wait":
            self._exact(captured, set())
            return PreparedBrowserAction(action, operation, risk, oid, "wait", ())
        if operation == "scroll":
            self._exact(captured, {"direction"})
            if captured["direction"] not in ("up", "down"):
                raise RuntimeStop("invalid_action")
            return PreparedBrowserAction(action, operation, risk, oid, f"scroll:{captured['direction']}",
                                         tuple(captured.items()))
        if operation == "go_back":
            self._exact(captured, {"to"})
            entry = self._table.get(("history", captured["to"]))
            if entry is None:
                raise RuntimeStop("invalid_action")
            _, _, _, doc = self.backend._run(self._capture())
            if doc["loader_id"] != self._doc.get("loader_id") or doc["previous_url"] != entry.destination:
                self._stale()
            return PreparedBrowserAction(action, operation, risk, oid, "history:back", (("to", "back"),),
                                         entry, entry.destination)
        kind, name = ACTION_KIND[operation]
        expected_keys = {name, "value"} if operation == "type_text" else {name}
        self._exact(captured, expected_keys)
        entry = self._table.get((kind, captured[name]))
        if entry is None:
            raise RuntimeStop("invalid_action")
        if entry.destination is not None and not self._origin_allowed(entry.destination):
            raise RuntimeStop("policy_denied")
        text = None
        if operation == "type_text":
            if self._registry is None or self._sink is None:
                raise RuntimeStop("adapter_contract_unsupported")
            try:
                resolved = self._registry.resolve(captured["value"])
            except ValueError:
                raise RuntimeStop("invalid_action") from None
        _, state, nodes, doc = self.backend._run(self._capture())
        if operation == "switch_tab":
            # What matters is the destination tab, not the page in front (which may be blank).
            tab = next((t for t in state.tabs or [] if str(getattr(t, "target_id", "")) == entry.tab_id), None)
            if tab is None or not self._origin_allowed(str(tab.url or "")):
                self._stale()
            params_fresh = (("tab", entry.key),)
        else:
            if doc["loader_id"] != self._doc.get("loader_id") or doc["frame_id"] != self._doc.get("frame_id") \
                    or not self._origin_allowed(str(doc["url"] or "")):
                self._stale()
            index, node = self._find(nodes, entry)
            if node is None or self._signature(node) != entry.signature:
                self._stale()
            if operation == "select_option":
                params_fresh = (("option", f"{index}:{entry.option_text}"),)
            elif operation == "type_text":
                params_fresh = (("field", index), ("value", captured["value"]))
                text = PreparedTextCommand(int(index), resolved)
            else:
                params_fresh = (("element", index),)
        return PreparedBrowserAction(action, operation, risk, oid, f"{oid}:{kind}:{entry.key}", params_fresh,
                                     entry, entry.destination, text)

    @staticmethod
    def _exact(params: dict, keys: set) -> None:
        if set(params) != keys:
            raise RuntimeStop("invalid_action")

    # ── execution ──
    def execute_prepared(self, prepared: PreparedBrowserAction, action_id: int):
        if type(prepared) is not PreparedBrowserAction:
            raise RuntimeStop("adapter_contract_unsupported")
        self._pending_effect = {"action_id": action_id, "operation": prepared.operation,
                                "url": self.report.get("url"), "signature": self.report.get("signature"),
                                "slot_ref": prepared.slot_ref,
                                "backend_node_id": prepared.target.backend_node_id if prepared.target else None}
        if prepared.action == "type_text":
            from reflexmesh.adapters.system_one.text_dispatch import dispatch_text

            if prepared._text is None or self._sink is None:
                raise RuntimeStop("adapter_contract_unsupported")
            return dispatch_text(self.backend, prepared._text, action_id, self._sink)
        if prepared._text is not None:
            raise RuntimeStop("adapter_contract_unsupported")
        params = dict(prepared._parameters)
        if prepared.action == "go_back":
            params = {}
        return self.backend.execute(prepared.action, params)

    def state_key(self, prepared: PreparedBrowserAction) -> str:
        """Identity of "this action on this page state", for the repeat check: the same target on
        an unchanged page is a repeat, the same button on a page that moved on is not. The target is
        named by what it is (role, name, option, destination, slot), never by Browser Use's index."""
        return f"{action_identity(prepared)}@{self.report.get('signature', '')}"

    def mutation_key(self, prepared: PreparedBrowserAction) -> str:
        """Identity of "this change on this document", for unknown-effect repeats (V0.6 section 9). A
        write changes what the page shows (a saved note appears in the list), so the page state is not
        the key: the same Save on the same order is the same operation however the page moved on."""
        parts = urlsplit(str(self.report.get("url") or ""))
        # The query can carry typed values (`?q=...`) and the journal keeps only paths: it goes in as a digest.
        query = hashlib.sha256(parts.query.encode()).hexdigest()[:12] if parts.query else ""
        return f"{action_identity(prepared)}@{parts.scheme}://{parts.netloc}{parts.path}?{query}"
