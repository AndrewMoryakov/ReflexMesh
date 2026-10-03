"""FixtureBrowser against a real Chromium and Browser Use (review 2026-09-25, section 2).

Runs only with Browser Use installed and REFLEXMESH_CHROME pointing at a Chromium executable;
otherwise the module is skipped and these checks remain pending for that environment.
"""

import importlib.util
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import RuntimeStop
from test_v05_runtime import sample

CHROME = os.environ.get("REFLEXMESH_CHROME")
AVAILABLE = bool(CHROME and Path(CHROME).exists() and importlib.util.find_spec("browser_use")
                 and importlib.util.find_spec("systemone_harness"))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(AVAILABLE, "set REFLEXMESH_CHROME and install Browser Use 0.13.10 + SystemOneHarness")
class RealBrowserAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser

        cls.port, cls.run_id = free_port(), "real-browser"
        cls.server = subprocess.Popen([sys.executable, str(ROOT / "experiments/v05/site/server.py"),
                                       "--port", str(cls.port), "--run-id", cls.run_id],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.origin = f"http://127.0.0.1:{cls.port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            try:
                opener.open(cls.origin + "/__state", timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        data = sample()
        data["fixture"] = {"origin": cls.origin, "run_id": cls.run_id}
        data["permissions"] = ["navigate", "type_text", "submit_form"]
        data["text_slots"].append({"id": "email", "version": 1, "value": "a@example.test"})
        cls.task = ExecutionTask.from_dict(data)
        cls.browser = FixtureBrowser(cls.task, chrome=CHROME)
        cls.browser.reset(cls.task.goal)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.server.terminate()
        cls.server.wait(timeout=5)

    def setUp(self):
        self.goto("/form")

    def goto(self, path):
        self.browser.backend._run(self.browser.backend._session.navigate_to(self.origin + path))
        time.sleep(0.5)

    def js(self, expression):
        async def run():
            cdp = await self.browser.backend._session.get_or_create_cdp_session()
            await cdp.cdp_client.send.Runtime.evaluate(params={"expression": expression, "returnByValue": True},
                                                       session_id=cdp.session_id)
        self.browser.backend._run(run())
        time.sleep(0.2)

    def offered(self, obs):
        """Fixture targets behind the candidates offered to the model."""
        return {self.browser.targets[i][1] for i in obs.candidates.get("elements", {}) if i in self.browser.targets}

    def index(self, target_id):
        return next(i for i, t in self.browser.targets.items() if t[1] == target_id)

    def test_identity_fields_and_fresh_session(self):
        obs = self.browser.observe()
        self.assertTrue(obs.fields["document_id"])
        self.assertTrue(self.browser.session_info()["fresh_profile"])
        self.assertFalse(self.browser.session_info()["attached"])
        offered = set(obs.fields["target_ids"])
        self.assertTrue({"name", "email", "send-form"} <= offered)
        send = self.index("send-form")
        self.assertIsNone(self.browser.admit("click", {"element": send}))

    def test_reload_and_navigation_change_document_identity(self):
        self.browser.observe()
        first = self.browser.page["document_id"]
        send = self.index("send-form")
        self.goto("/form")
        self.assertEqual(self.browser.admit("click", {"element": send}), "stale_target")
        self.browser.observe()
        self.assertNotEqual(self.browser.page["document_id"], first)
        self.goto("/settings")
        self.browser.observe()
        self.assertNotIn("send-form", [t[1] for t in self.browser.targets.values()])

    def test_replaced_node_with_same_fixture_id_is_stale(self):
        self.browser.observe()
        send = self.index("send-form")
        self.js("var b=document.querySelector('[data-reflex-id=\"send-form\"]'); b.replaceWith(b.cloneNode(true));")
        self.assertEqual(self.browser.admit("click", {"element": send}), "stale_target")

    def test_disabled_target_is_stale_then_not_offered(self):
        self.browser.observe()
        send = self.index("send-form")
        self.js("document.querySelector('[data-reflex-id=\"send-form\"]').disabled = true;")
        self.assertEqual(self.browser.admit("click", {"element": send}), "stale_target")
        obs = self.browser.observe()
        self.assertNotIn("send-form", self.offered(obs))

    def test_changed_form_endpoint_is_stale_then_denied(self):
        self.browser.observe()
        send = self.index("send-form")
        self.js("document.querySelector('[data-reflex-id=\"send-form\"]').setAttribute('formaction', '/danger/delete');")
        self.assertEqual(self.browser.admit("click", {"element": send}), "stale_target")
        obs = self.browser.observe()
        self.assertNotIn("send-form", self.offered(obs))
        resend = self.index("send-form")
        self.assertEqual(self.browser.admit("click", {"element": resend}), "policy_denied")

    def test_frames_are_unsupported(self):
        self.js("var f=document.createElement('iframe'); f.src='/reports'; document.body.appendChild(f);")
        time.sleep(0.5)
        with self.assertRaises(RuntimeStop) as error:
            self.browser.observe()
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")

    def test_duplicate_fixture_id_is_unsupported(self):
        self.js("var b=document.querySelector('[data-reflex-id=\"send-form\"]');"
                "b.parentNode.appendChild(b.cloneNode(true));")
        with self.assertRaises(RuntimeStop) as error:
            self.browser.observe()
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")


if __name__ == "__main__":
    unittest.main()
