"""Production ownership/reset producer with narrow fake pinned SOH/CDP I/O.

The claim/session HTTP protocol and FixtureBrowser checks are real. No browser,
Browser Use installation, model, or fabricated positive assessment is involved.
This is regression source, not evidence that the pinned SDK accepts the path.
"""

import asyncio
import json
import multiprocessing as mp
import shutil
import stat
import sys
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.adapters.system_one.harness import HarnessStrategy
from reflexmesh.runtime.ownership import FixtureOwnership
from reflexmesh.runtime.runner import RuntimeStop
from reflexmesh.text.slots import SlotRegistry
from test_v05_fixture import FixtureServerCase


class PinnedNetwork:
    def __init__(self, backend):
        self.backend = backend

    async def setCookie(self, params, session_id=None):
        backend = self.backend
        # This assertion examines the real client after its real HTTP bind.
        # A fake backend cannot manufacture the authoritative claim binding.
        assert backend.owner.binding["session_id"] == backend._session.id
        assert backend.owner.binding["profile_id"]
        assert session_id == "fake-cdp-session"
        backend.calls.append("setCookie")
        backend.cookie_params = dict(params)
        if backend.cookie_exception:
            raise ValueError("CDP validation rejected " + params["value"])
        return backend.cookie_response

    async def getCookies(self, params=None, session_id=None):
        backend = self.backend
        assert params == {"urls": [backend.task.origin + "/"]}
        assert session_id == "fake-cdp-session"
        backend.calls.append("getCookies")
        if backend.cookie_readback is not None:
            return backend.cookie_readback
        cookie = backend.cookie_params
        return {"cookies": [{
            "name": cookie["name"], "value": cookie["value"], "domain": "127.0.0.1",
            "path": cookie["path"], "httpOnly": cookie["httpOnly"],
            "sameSite": cookie["sameSite"], "session": "expires" not in cookie,
        }]}


class PinnedSession:
    def __init__(self, backend):
        self.backend = backend
        self.id = str(uuid.uuid4())
        self.browser_profile = SimpleNamespace(user_data_dir=Path(backend.user_data_dir),
                                               is_local=True, use_cloud=False)
        self.cdp_url = "ws://127.0.0.1:9222/devtools/browser/fake-local-browser"
        self.cdp = SimpleNamespace(
            session_id="fake-cdp-session",
            cdp_client=SimpleNamespace(send=SimpleNamespace(Network=PinnedNetwork(backend)),
                                       ws=SimpleNamespace(debug=False)))

    async def get_or_create_cdp_session(self):
        return self.cdp

    async def get_selector_map(self):
        return self.backend.nodes


class PinnedBackend:
    """Only the reviewed constructor/start/reset/read/CDP surface is emulated."""

    def __init__(self, task, owner, **kwargs):
        self.task, self.owner = task, owner
        self.kwargs = kwargs
        self.cdp_url = kwargs["cdp_url"]
        self.user_data_dir = kwargs["user_data_dir"]
        self.start_url = kwargs["start_url"]
        self.text_values = kwargs["text_values"]
        self._session = None
        self.calls = ["construct"]
        self.cookie_params = None
        self.cookie_response = {"success": True}
        self.cookie_readback = None
        self.cookie_exception = False
        self.after_start = self.during_observe = self.during_reset = self.during_execute = None
        self.close_error = False
        self.closed = False
        self.nodes = {
            0: SimpleNamespace(attributes={"data-reflex-id": "send-form"},
                               backend_node_id=15, tag_name="button"),
            1: SimpleNamespace(attributes={"data-reflex-id": "name"},
                               backend_node_id=16, tag_name="input"),
        }

    def _run(self, coroutine):
        return asyncio.run(coroutine)

    async def _start(self):
        self.calls.append("start")
        if self._session is None:
            self._session = PinnedSession(self)
        if self.after_start:
            self.after_start(self)

    def reset(self, goal):
        assert self.cookie_params is not None, "fixture navigation preceded cookie installation"
        self.calls.append("reset")
        request = urllib.request.Request(self.start_url, headers={
            "Cookie": "ReflexMeshOwner=" + self.cookie_params["value"],
        })
        with urllib.request.urlopen(request, timeout=1) as response:
            assert response.status == 200
        if self.during_reset:
            self.during_reset(self)

    def observe(self):
        self.calls.append("observe")
        if self.during_observe:
            self.during_observe(self)
        return SimpleNamespace(text="Fixture controls", fields={"url": self.start_url}, candidates={
            "elements": {"0": "Send"}, "text_fields": {"1": "Name"},
            "text_values": dict(self.text_values),
        })

    def execute(self, action, params):
        self.calls.append("execute")
        if self.during_execute:
            self.during_execute(self)
        return SimpleNamespace(ok=True)

    def close(self):
        self.calls.append("close")
        if self.close_error:
            raise OSError("close failed " + self.owner.browser_secret)
        self.closed = True


@contextmanager
def fake_pinned_backend(task, owner, *, configure=None):
    """Patch the optional dependency only, preserving all ownership producers."""
    made = []

    def factory(**kwargs):
        backend = PinnedBackend(task, owner, **kwargs)
        made.append(backend)
        if configure is not None:
            configure(backend)
        return backend

    package = ModuleType("systemone_harness")
    package.__path__ = []
    envs = ModuleType("systemone_harness.envs")
    envs.__path__ = []
    browser = ModuleType("systemone_harness.envs.browser")
    browser.BrowserEnvironment = factory
    package.envs, envs.browser = envs, browser
    with patch.dict(sys.modules, {
        "systemone_harness": package, "systemone_harness.envs": envs,
        "systemone_harness.envs.browser": browser,
    }):
        yield made


def lose_session(backend):
    backend._session = None


class BrowserOwnership(FixtureServerCase):
    def setUp(self):
        super().setUp()
        self.execution_task = self.task()
        self.owner = FixtureOwnership(mp.get_context("fork"), self.execution_task, str(uuid.uuid4()))
        self.browsers = []

    def tearDown(self):
        try:
            for browser in self.browsers:
                # Every backend in this file is fake. The test knows there is
                # no live browser and can remove its own private temp fixture.
                path = browser._profile_path
                if path is not None:
                    shutil.rmtree(path, ignore_errors=True)
        finally:
            super().tearDown()

    def browser(self, *, acquire=True):
        if acquire:
            self.owner.acquire(1)
        browser = FixtureBrowser(self.execution_task)
        self.browsers.append(browser)
        browser.bind_ownership(self.owner)
        return browser

    def public_state(self):
        with urllib.request.urlopen(self.origin + "/__state", timeout=1) as response:
            return json.load(response)

    def test_no_claim_creates_no_backend_and_sends_no_cookie(self):
        with fake_pinned_backend(self.execution_task, self.owner) as made:
            browser = FixtureBrowser(self.execution_task)
            self.browsers.append(browser)
            self.assertIsNone(browser.backend)
            with self.assertRaises(RuntimeStop) as error:
                browser.bind_ownership(self.owner)
        self.assertEqual(error.exception.reason, "ownership_unavailable")
        self.assertEqual(made, [])
        self.assertIsNone(browser._profile_path)
        self.assertEqual(self.public_state()["phase"], "unclaimed")

    def test_duck_typed_owner_is_rejected_before_construction(self):
        self.owner.acquire(1)
        forged = SimpleNamespace(binding=self.owner.binding, browser_secret=self.owner.browser_secret,
                                 bind_session=self.owner.bind_session, assert_session=self.owner.assert_session)
        with fake_pinned_backend(self.execution_task, self.owner) as made:
            browser = FixtureBrowser(self.execution_task)
            with self.assertRaises(RuntimeStop):
                browser.bind_ownership(forged)
        self.assertEqual(made, [])

    def test_each_backend_has_new_private_explicit_profile(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            first = self.browser()
            self.assertIsNone(first.backend)
            self.assertIsNone(first._profile_path)
            first.reset("First attempt")
            first.close()
        # These are fake backends with no live process; the test can confirm
        # closure and release this claim before starting a distinct attempt.
        self.owner.revoke(1)
        self.owner.release("closed", 1)
        self.owner = FixtureOwnership(mp.get_context("fork"), self.execution_task, str(uuid.uuid4()))
        with fake_pinned_backend(self.execution_task, self.owner):
            second = self.browser()
            self.assertIsNone(second.backend)
            self.assertIsNone(second._profile_path)
            second.reset("Second attempt")
        self.assertNotEqual(first._profile_path, second._profile_path)
        self.assertNotEqual(first._profile_id, second._profile_id)
        for browser in (first, second):
            path = browser._profile_path
            self.assertTrue(path.is_dir())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertIn("browser-use-user-data-dir-reflexmesh-", path.name)
            self.assertEqual(Path(browser.backend.user_data_dir), path)
            self.assertIsNone(browser.backend.cdp_url)
            self.assertEqual(browser.backend.text_values, {s.reference: s.reference
                                                          for s in self.execution_task.slots})
            if not browser._closed:
                browser.close()
            self.assertTrue(path.exists(), "browser must not delete a possibly-live profile")
        self.assertEqual(self.public_state()["phase"], "active")

    def test_reset_binds_actual_session_and_cookie_before_first_navigation(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset(self.execution_task.goal)
            observation = browser.observe()
        backend = browser.backend
        self.assertEqual(backend.calls[:5], ["construct", "start", "setCookie", "getCookies", "reset"])
        self.assertEqual(self.owner.binding["session_id"], backend._session.id)
        self.assertEqual(self.owner.binding["profile_id"], browser._profile_id)
        self.assertEqual(observation.fields["ownership"], self.owner.binding)
        self.assertEqual(self.public_state()["log"][-1]["binding"], self.owner.binding)
        params = backend.cookie_params
        self.assertEqual(params["name"], "ReflexMeshOwner")
        self.assertEqual(params["url"], self.execution_task.origin)
        self.assertIs(params["httpOnly"], True)
        self.assertEqual(params["sameSite"], "Strict")
        self.assertNotIn("expires", params)
        public = json.dumps({"fields": observation.fields, "candidates": observation.candidates,
                             "text": observation.text, "state": self.public_state()})
        self.assertNotIn(self.owner.browser_secret, public)
        self.assertNotIn(str(browser._profile_path), public)
        observation.fields["ownership"]["profile_id"] = "caller-reported-profile"
        self.assertEqual(browser.observe().fields["ownership"], self.owner.binding)

    def test_repeat_reset_keeps_one_session_and_does_not_install_again(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("First")
            identity = dict(self.owner.binding)
            browser.reset("Second")
        self.assertEqual(self.owner.binding, identity)
        self.assertEqual(browser.backend.calls.count("start"), 1)
        self.assertEqual(browser.backend.calls.count("setCookie"), 1)
        self.assertEqual(browser.backend.calls.count("reset"), 2)

    def test_binding_and_bootstrap_admission_allocate_nothing(self):
        with fake_pinned_backend(self.execution_task, self.owner) as made, patch(
                "reflexmesh.adapters.system_one.fixture_browser.tempfile.mkdtemp") as allocate:
            browser = self.browser()
            browser.bind_ownership(self.owner)
            self.assertIsNone(browser.admit("navigate", {"bootstrap": True}))
            self.assertIsNone(browser.backend)
            self.assertIsNone(browser._profile_path)
            browser.close()
            with self.assertRaises(RuntimeStop):
                browser.reset("Closed before start")
            allocate.assert_not_called()
        self.assertEqual(made, [])

    def test_nonbootstrap_entrypoints_cannot_start_an_enrolled_browser(self):
        self.owner.acquire(1)
        operations = (lambda b: b.observe(), lambda b: b.admit("click", {"element": "0"}),
                      lambda b: b.execute("click", {"element": "0"}),
                      lambda b: b.execute_prepared(None, 1))
        with fake_pinned_backend(self.execution_task, self.owner) as made, patch(
                "reflexmesh.adapters.system_one.fixture_browser.tempfile.mkdtemp") as allocate:
            for operation in operations:
                browser = self.browser(acquire=False)
                with self.subTest(operation=operation), self.assertRaises(RuntimeStop) as error:
                    operation(browser)
                self.assertEqual(error.exception.reason, "ownership_unavailable")
                self.assertIsNone(browser.backend)
                self.assertIsNone(browser._profile_path)
            allocate.assert_not_called()
        self.assertEqual(made, [])

    def test_repeated_harness_setup_failures_allocate_no_profile_or_backend(self):
        self.owner.acquire(1)
        for stage in ("provider", "action-space", "controller-init", "controller-run"):
            for retry in range(3):
                with self.subTest(stage=stage, retry=retry):
                    browser = FixtureBrowser(self.execution_task)
                    self.browsers.append(browser)
                    gate = SimpleNamespace(
                        fixture_owner=self.owner, max_steps=1,
                        slot_registry=SlotRegistry.from_task(self.execution_task),
                        slot_sink=SimpleNamespace(handoff=lambda *args, **kwargs: None))
                    provider = SimpleNamespace(closed=False)

                    def close_provider():
                        provider.closed = True

                    provider.close = close_provider

                    def provider_factory():
                        self.assertIs(browser._fixture_owner, self.owner)
                        self.assertIsNone(browser.backend)
                        self.assertIsNone(browser._profile_path)
                        if stage == "provider":
                            raise RuntimeError(stage + " setup failed")
                        return provider

                    def space_factory():
                        if stage == "action-space":
                            raise RuntimeError(stage + " setup failed")
                        return None

                    class SetupController:
                        def __init__(self, *args, **kwargs):
                            if stage == "controller-init":
                                raise RuntimeError(stage + " setup failed")

                        def run(self, goal):
                            # A controller failure before it calls reset must
                            # not allocate browser resources either.
                            raise RuntimeError(stage + " setup failed")

                    controller = ModuleType("systemone_harness.controller")
                    controller.Controller = SetupController
                    strategy = HarnessStrategy("Start", lambda: browser, space_factory,
                                               provider_factory, None)
                    with fake_pinned_backend(self.execution_task, self.owner) as made, patch.dict(
                            sys.modules, {"systemone_harness.controller": controller}), patch(
                            "reflexmesh.adapters.system_one.fixture_browser.tempfile.mkdtemp") as allocate:
                        with self.assertRaisesRegex(RuntimeError, stage + " setup failed"):
                            strategy(gate, SimpleNamespace(put=lambda value: None))
                        allocate.assert_not_called()
                    self.assertEqual(made, [])
                    self.assertIsNone(browser.backend)
                    self.assertIsNone(browser._profile_path)
                    self.assertTrue(browser._closed)
                    self.assertEqual(provider.closed, stage != "provider")
        self.assertEqual(self.public_state()["phase"], "active")

    def test_missing_session_schema_blocks_before_cookie_or_navigation(self):
        mutations = (
            lose_session,
            lambda b: delattr(b._session, "id"),
            lambda b: delattr(b._session, "browser_profile"),
            lambda b: setattr(b._session.browser_profile, "user_data_dir", None),
            lambda b: setattr(b._session.browser_profile, "user_data_dir", str(Path(b.user_data_dir).parent)),
            lambda b: setattr(b._session.browser_profile, "is_local", False),
            lambda b: setattr(b._session.browser_profile, "use_cloud", True),
        )
        # Each failure before bind leaves the real claim available for the next
        # distinct fake browser; none fabricates or bypasses ownership state.
        self.owner.acquire(1)
        with fake_pinned_backend(self.execution_task, self.owner,
                                 configure=lambda b: setattr(b, "after_start", mutation)):
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    browser = self.browser(acquire=False)
                    with self.assertRaises(RuntimeStop) as error:
                        browser.reset("Start")
                    self.assertEqual(error.exception.reason, "ownership_unavailable")
                    self.assertNotIn("setCookie", browser.backend.calls)
                    self.assertNotIn("reset", browser.backend.calls)
        self.assertEqual(self.public_state()["log"], [])
        self.assertEqual(self.owner.view().status, "unknown")

    def test_missing_cdp_cookie_method_blocks_navigation(self):
        def configure(backend):
            backend.after_start = lambda b: setattr(b._session.cdp.cdp_client.send,
                                                    "Network", SimpleNamespace())

        with fake_pinned_backend(self.execution_task, self.owner, configure=configure):
            browser = self.browser()
            with self.assertRaises(RuntimeStop) as error:
                browser.reset("Start")
        self.assertEqual(error.exception.reason, "ownership_unavailable")
        self.assertNotIn("setCookie", browser.backend.calls)
        self.assertNotIn("reset", browser.backend.calls)

    def test_missing_cookie_response_schema_is_not_success(self):
        with fake_pinned_backend(self.execution_task, self.owner,
                                 configure=lambda b: setattr(b, "cookie_response", {})):
            browser = self.browser()
            with self.assertRaises(RuntimeStop):
                browser.reset("Start")
        self.assertNotIn("reset", browser.backend.calls)

    def test_missing_cookie_readback_schema_is_not_success(self):
        with fake_pinned_backend(self.execution_task, self.owner,
                                 configure=lambda b: setattr(b, "cookie_readback", {"cookies": []})):
            browser = self.browser()
            with self.assertRaises(RuntimeStop):
                browser.reset("Start")
        self.assertNotIn("reset", browser.backend.calls)

    def test_cookie_exception_never_exposes_secret(self):
        with fake_pinned_backend(self.execution_task, self.owner,
                                 configure=lambda b: setattr(b, "cookie_exception", True)):
            browser = self.browser()
            with self.assertRaises(RuntimeStop) as error:
                browser.reset("Start")
        self.assertEqual(str(error.exception), "ownership_unavailable")
        self.assertTrue(error.exception.__suppress_context__)
        self.assertNotIn("reset", browser.backend.calls)

    def test_debug_cdp_transport_blocks_cookie_handoff(self):
        def configure(backend):
            backend.after_start = lambda b: setattr(b._session.cdp.cdp_client.ws, "debug", True)

        with fake_pinned_backend(self.execution_task, self.owner, configure=configure):
            browser = self.browser()
            with self.assertRaises(RuntimeStop):
                browser.reset("Start")
        self.assertNotIn("setCookie", browser.backend.calls)
        self.assertNotIn("reset", browser.backend.calls)

    def test_changed_debug_transport_blocks_later_backend_calls(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            browser.backend._session.cdp.cdp_client.ws.debug = True
            before = list(browser.backend.calls)
            with self.assertRaises(RuntimeStop):
                browser.observe()
        self.assertEqual(browser.backend.calls, before)

    def test_changed_external_connection_configuration_has_failure_witness(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            browser.backend.cdp_url = "http://unexpected-browser.example"
            with self.assertRaises(RuntimeStop):
                browser.observe()
        self.assertEqual(self.owner.view().status, "fail")
        self.assertNotIn("unexpected-browser.example", json.dumps(self.owner.export()))

    def test_changed_session_stops_each_browser_entrypoint(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            browser.observe()
            prepared = browser.prepare("click", {"element": "0"})
            backend = browser.backend
            backend._session.id = "replacement-session"
            before = list(backend.calls)
            operations = (browser.observe, lambda: browser.admit("click", {"element": "0"}),
                          lambda: browser.execute_prepared(prepared, 1),
                          lambda: browser.execute("click", {"element": "0"}),
                          lambda: browser.reset("Again"))
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(RuntimeStop) as error:
                    operation()
                self.assertEqual(error.exception.reason, "ownership_lost")
            self.assertEqual(backend.calls, before)
        self.assertEqual(self.owner.view().status, "fail")
        self.owner.read(1)
        self.assertEqual(self.owner.view().status, "fail", "later consistent server data cannot erase drift")

    def test_present_but_changed_profile_has_durable_redacted_failure(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            browser.backend._session.browser_profile.user_data_dir = browser._profile_path.parent
            with self.assertRaises(RuntimeStop) as error:
                browser.observe()
        self.assertEqual(error.exception.reason, "ownership_lost")
        self.assertEqual(self.owner.view().status, "fail")
        records = self.owner.export()
        self.assertTrue(any(row.get("boundary") == "fixture_browser.private_session_guard/v1"
                            for row in records))
        public = json.dumps(records)
        self.assertNotIn(str(browser._profile_path), public)
        self.assertNotIn(self.owner.browser_secret, public)

    def test_malformed_session_id_cannot_publish_the_cookie_secret(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            browser.backend._session.id = self.owner.browser_secret
            with self.assertRaises(RuntimeStop):
                browser.observe()
        self.assertEqual(self.owner.view().status, "fail")
        self.assertNotIn(self.owner.browser_secret, json.dumps(self.owner.export()))

    def test_profile_changed_during_observation_cannot_produce_public_binding(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            browser.backend.during_observe = lambda b: setattr(b._session.browser_profile,
                                                              "user_data_dir", None)
            with self.assertRaises(RuntimeStop) as error:
                browser.observe()
        self.assertEqual(error.exception.reason, "ownership_lost")
        self.assertEqual(self.owner.view().status, "unknown", "missing SDK state is not proof of a violation")

    def test_session_lost_inside_reset_is_detected(self):
        with fake_pinned_backend(self.execution_task, self.owner,
                                 configure=lambda b: setattr(b, "during_reset", lose_session)):
            browser = self.browser()
            with self.assertRaises(RuntimeStop) as error:
                browser.reset("Start")
        self.assertEqual(error.exception.reason, "ownership_lost")

    def test_session_lost_inside_prepared_execute_is_detected(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            browser.observe()
            command = browser.prepare("click", {"element": "0"})
            browser.backend.during_execute = lose_session
            with self.assertRaises(RuntimeStop) as error:
                browser.execute_prepared(command, 1)
        self.assertEqual(error.exception.reason, "ownership_lost")

    def test_close_error_keeps_claim_and_private_profile_for_supervisor(self):
        with fake_pinned_backend(self.execution_task, self.owner):
            browser = self.browser()
            browser.reset("Start")
            binding = dict(self.owner.binding)
            browser.backend.close_error = True
            with self.assertRaises(RuntimeStop) as error:
                browser.close()
        self.assertEqual(str(error.exception), "ownership_lost")
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual(self.owner.binding, binding)
        self.assertEqual(self.public_state()["phase"], "active")
        self.assertTrue(browser._profile_path.exists())
        with self.assertRaises(RuntimeStop):
            browser.observe()

