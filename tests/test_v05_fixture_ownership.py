"""Real HTTP regression sources for attempt-owned fixture state and effects.

These tests exercise the server routes, rather than client mocks or fabricated
positive ownership evidence. No browser package or model is required.
"""

import copy
import http.client
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class HttpFixture:
    def __init__(self, run_id="ownership-fixture"):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.run_id = run_id
        self.origin = f"http://127.0.0.1:{self.port}"
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "experiments/v05/site/server.py"),
             "--port", str(self.port), "--run-id", self.run_id, "--slow-seconds", "3"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        expires = time.monotonic() + 5
        while time.monotonic() < expires:
            if self.process.poll() is not None:
                raise RuntimeError("fixture exited before readiness")
            try:
                if self.request("/__state", timeout=0.1)[0] == 200:
                    return self
            except OSError:
                time.sleep(0.01)
        self.stop()
        raise RuntimeError("fixture readiness timeout")

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.process = None

    def request(self, path, *, payload=None, form=None, cookie=None, headers=None, timeout=5):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie is not None:
            request_headers["Cookie"] = f"ReflexMeshOwner={cookie}"
        request = urllib.request.Request(self.origin + path, data=body, headers=request_headers)
        try:
            response = urllib.request.build_opener(NoRedirect).open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            value = json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
            return response.code, value


class FixtureOwnership(unittest.TestCase):
    def setUp(self):
        self.fixture = HttpFixture()
        self.addCleanup(self.fixture.stop)
        self.fixture.start()

    def credentials(self, *, attempt="attempt-1", fixture=None):
        fixture = fixture or self.fixture
        return {"identity": {"task_id": "task", "task_revision": 1, "attempt_id": attempt,
                             "run_id": fixture.run_id, "claim_id": secrets.token_hex(16)},
                "owner_secret": secrets.token_urlsafe(32), "browser_secret": secrets.token_urlsafe(32)}

    def acquire(self, credentials=None, *, fixture=None, bind=True):
        fixture = fixture or self.fixture
        credentials = credentials or self.credentials(fixture=fixture)
        status, receipt = fixture.request("/__claim", payload=credentials)
        self.assertEqual(status, 200)
        binding = receipt["binding"]
        if bind:
            status, receipt = fixture.request("/__owner/bind", payload={
                "owner_secret": credentials["owner_secret"], "binding": binding,
                "session_id": "session-" + credentials["identity"]["attempt_id"],
                "profile_id": "profile-" + credentials["identity"]["attempt_id"]})
            self.assertEqual(status, 200)
            binding = receipt["binding"]
        return credentials, binding, receipt

    def owner(self, path, credentials, binding=None, *, fixture=None, **extra):
        payload = {"owner_secret": credentials["owner_secret"], **extra}
        if binding is None:
            payload["identity"] = credentials["identity"]
        else:
            payload["binding"] = binding
        return (fixture or self.fixture).request(path, payload=payload)

    def read_owner(self, credentials, binding=None, *, fixture=None):
        status, snapshot = self.owner("/__owner/state", credentials, binding, fixture=fixture)
        self.assertEqual(status, 200)
        return snapshot

    def release(self, credentials, binding, *, fixture=None):
        self.assertEqual(self.owner("/__owner/revoke", credentials, binding, fixture=fixture)[0], 200)
        status, value = self.owner("/__owner/release", credentials, binding,
                                   fixture=fixture, cleanup_confirmed=True)
        self.assertEqual(status, 200)
        self.assertEqual(value["phase"], "released")
        return value

    def test_unclaimed_browser_requests_have_no_effect_or_log(self):
        for path in ("/", "/reports", "/form", "/favicon.ico"):
            self.assertEqual(self.fixture.request(path)[0], 403)
        for path in ("/settings", "/form", "/danger/delete", "/slow/export"):
            self.assertEqual(self.fixture.request(path, form={})[0], 403)
        _, value = self.fixture.request("/__state")
        self.assertEqual(value["phase"], "unclaimed")
        self.assertIsNone(value["binding"])
        self.assertEqual(value["sequence"], 0)
        self.assertEqual(value["log"], [])
        self.assertEqual(value["state"]["exports"], 0)

    def test_simultaneous_claims_have_exactly_one_winner(self):
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def claim(credentials):
            try:
                barrier.wait(timeout=2)
                results.append(self.fixture.request("/__claim", payload=credentials))
            except Exception as error:
                errors.append(type(error).__name__)

        threads = [threading.Thread(target=claim, args=(self.credentials(attempt=f"attempt-{n}"),))
                   for n in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(code for code, _ in results), [200, 409])
        winner = next(value for code, value in results if code == 200)
        self.assertEqual(winner["baseline"]["sequence"], 0)
        self.assertEqual(winner["binding"]["owner_epoch"], 1)

    def test_duplicate_and_new_claims_cannot_steal_active_owner(self):
        credentials, binding, _ = self.acquire()
        self.assertEqual(self.fixture.request("/__claim", payload=credentials)[0], 409)
        self.assertEqual(self.fixture.request("/__claim", payload=self.credentials(attempt="other"))[0], 409)
        self.assertEqual(self.read_owner(credentials, binding)["binding"], binding)

    def test_distinct_fixture_processes_are_independent(self):
        second = HttpFixture(run_id=self.fixture.run_id)
        self.addCleanup(second.stop)
        second.start()
        first_credentials, first_binding, _ = self.acquire()
        second_credentials, second_binding, _ = self.acquire(fixture=second)
        self.assertNotEqual(first_binding["instance_id"], second_binding["instance_id"])
        self.assertEqual(first_binding["owner_epoch"], second_binding["owner_epoch"])
        self.assertEqual(self.fixture.request("/settings", form={"notify_email": "on"},
                                             cookie=first_credentials["browser_secret"])[0], 303)
        self.assertEqual(self.read_owner(second_credentials, second_binding, fixture=second)
                         ["state"]["settings_saved"], 0)
        self.assertEqual(second.request("/settings", form={}, cookie=first_credentials["browser_secret"])[0], 403)

    def test_cookie_requires_actual_session_binding_and_never_owner_secret(self):
        credentials, binding, _ = self.acquire(bind=False)
        self.assertEqual(self.fixture.request("/form", cookie=credentials["browser_secret"])[0], 403)
        self.assertEqual(self.fixture.request("/form", form={}, cookie=credentials["browser_secret"])[0], 403)
        for session_id, profile_id in (("", "profile"), ("session", "/tmp/profile"),
                                        ("session", "C:\\profile"), ("session", "")):
            self.assertEqual(self.owner("/__owner/bind", credentials, binding,
                                        session_id=session_id, profile_id=profile_id)[0], 400)
        status, bound = self.owner("/__owner/bind", credentials, binding,
                                   session_id="actual-session", profile_id="actual-profile")
        self.assertEqual(status, 200)
        self.assertEqual(self.owner("/__owner/bind", credentials, bound["binding"],
                                    session_id="another-session", profile_id="another-profile")[0], 409)
        for cookie in (None, secrets.token_urlsafe(32), credentials["owner_secret"]):
            self.assertEqual(self.fixture.request("/form", form={}, cookie=cookie)[0], 403)
        self.assertEqual(self.fixture.request("/form", cookie=credentials["browser_secret"])[0], 200)

    def test_form_effect_is_atomic_bound_and_redacted_publicly(self):
        credentials, binding, receipt = self.acquire()
        form = {"name": "private-form-name", "email": "private-form@example.test"}
        self.assertEqual(self.fixture.request("/form", form=form, cookie=credentials["browser_secret"])[0], 303)
        private = self.read_owner(credentials, binding)
        self.assertEqual(private["state"]["form"], form)
        self.assertEqual(private["sequence"], receipt["baseline"]["sequence"] + 1)
        self.assertEqual(len(private["effects"]), 1)
        self.assertEqual(private["effects"][0]["binding"], binding)
        self.assertEqual(private["effects"][0]["path"], "/form")
        self.assertEqual(private["log"], private["effects"])
        self.assertNotIn("data", private["log"][0])
        self.assertEqual(private["baseline"]["state"]["form"], None)
        _, public = self.fixture.request("/__state")
        self.assertEqual(public["state"]["form"], {"submitted": True})
        for value in (*form.values(), credentials["owner_secret"], credentials["browser_secret"]):
            self.assertNotIn(value, json.dumps(public))
            self.assertNotIn(value, json.dumps(private["log"]))
        for secret in (credentials["owner_secret"], credentials["browser_secret"]):
            self.assertNotIn(secret, json.dumps(private))

    def test_recovery_after_lost_claim_and_bind_response_keeps_atomic_baseline(self):
        credentials, initial_binding, receipt = self.acquire(bind=False)
        recovered = self.read_owner(credentials)
        self.assertEqual(recovered["binding"], initial_binding)
        self.assertEqual(recovered["baseline"], receipt["baseline"])
        status, bound = self.owner("/__owner/bind", credentials, initial_binding,
                                   session_id="session", profile_id="profile")
        self.assertEqual(status, 200)
        self.assertEqual(self.owner("/__owner/state", credentials, initial_binding)[0], 403)
        self.assertEqual(self.read_owner(credentials)["binding"], bound["binding"])
        self.assertEqual(self.fixture.request("/reports", cookie=credentials["browser_secret"])[0], 200)
        status, revoked = self.owner("/__owner/revoke", credentials)
        self.assertEqual(status, 200)
        self.assertEqual(revoked["phase"], "quarantined")
        self.assertEqual(revoked["baseline"], receipt["baseline"])
        self.assertEqual(self.owner("/__owner/release", credentials, cleanup_confirmed=True)[0], 403)

    def test_unknown_cleanup_cannot_release_or_enable_takeover(self):
        credentials, binding, _ = self.acquire()
        self.assertEqual(self.owner("/__owner/release", credentials, binding, cleanup_confirmed=True)[0], 409)
        self.assertEqual(self.owner("/__owner/revoke", credentials, binding)[0], 200)
        for value in (None, False, "clean", "unknown", 1):
            status, response = self.owner("/__owner/release", credentials, binding, cleanup_confirmed=value)
            self.assertEqual(status, 409)
            self.assertEqual(response["error"], "cleanup_not_confirmed")
        self.assertEqual(self.fixture.request("/settings", form={}, cookie=credentials["browser_secret"])[0], 403)
        self.assertEqual(self.fixture.request("/__claim", payload=self.credentials(attempt="new"))[0], 409)
        self.assertEqual(self.read_owner(credentials, binding)["phase"], "quarantined")

    def test_clean_release_increments_epoch_and_rejects_stale_attempt(self):
        credentials, binding, _ = self.acquire()
        released = self.release(credentials, binding)
        self.assertEqual(released["binding"], binding)
        new_credentials, new_binding, _ = self.acquire(self.credentials(attempt="attempt-2"))
        self.assertEqual(new_binding["instance_id"], binding["instance_id"])
        self.assertEqual(new_binding["owner_epoch"], binding["owner_epoch"] + 1)
        current_before = self.read_owner(new_credentials, new_binding)
        self.assertEqual(self.owner("/__owner/state", credentials, binding)[0], 403)
        self.assertEqual(self.owner("/__owner/revoke", credentials, binding)[0], 403)
        status, stale_revoke = self.owner("/__owner/revoke", credentials)
        self.assertEqual(status, 200)
        self.assertEqual(stale_revoke["phase"], "revoked_before_claim")
        current_after = self.read_owner(new_credentials, new_binding)
        self.assertEqual(current_after["phase"], "active")
        self.assertEqual(current_after["binding"], new_binding)
        self.assertEqual(current_after, current_before)
        self.assertEqual(self.fixture.request("/__claim", payload=credentials)[0], 409)
        self.assertEqual(self.fixture.request("/form", form={}, cookie=credentials["browser_secret"])[0], 403)
        for field, stale in (("owner_epoch", binding["owner_epoch"]), ("attempt_id", binding["attempt_id"]),
                              ("session_id", binding["session_id"]), ("task_revision", True)):
            forged = {**new_binding, field: stale}
            self.assertEqual(self.owner("/__owner/state", new_credentials, forged)[0], 403)

    def test_restart_same_origin_same_run_rejects_old_instance(self):
        credentials, binding, _ = self.acquire()
        self.fixture.stop()
        self.fixture.start()
        new_credentials, new_binding, _ = self.acquire(self.credentials(attempt="attempt-2"))
        self.assertNotEqual(new_binding["instance_id"], binding["instance_id"])
        self.assertEqual(self.owner("/__owner/state", credentials, binding)[0], 403)
        stale_instance = {**new_binding, "instance_id": binding["instance_id"]}
        self.assertEqual(self.owner("/__owner/state", new_credentials, stale_instance)[0], 403)
        self.assertEqual(self.fixture.request("/danger/delete", form={}, cookie=credentials["browser_secret"])[0], 403)
        self.assertFalse(self.read_owner(new_credentials, new_binding)["state"]["account_deleted"])

    def test_reset_changes_instance_without_rewinding_process_epoch(self):
        # Loading this fixture is part of the test, not a second implementation.
        spec = importlib.util.spec_from_file_location("ownership_http_fixture",
                                                     ROOT / "experiments/v05/site/server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.RUN_ID = "reset-fixture"
        module.reset()
        local = HttpFixture(run_id=module.RUN_ID)
        with module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler) as server:
            local.port = server.server_port
            local.origin = f"http://127.0.0.1:{local.port}"
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                credentials, binding, _ = self.acquire(fixture=local)
                module.reset()
                new_credentials, new_binding, _ = self.acquire(fixture=local)
                self.assertNotEqual(new_binding["instance_id"], binding["instance_id"])
                self.assertEqual(new_binding["owner_epoch"], binding["owner_epoch"] + 1)
                self.assertEqual(self.owner("/__owner/revoke", credentials, binding, fixture=local)[0], 403)
                self.assertEqual(local.request("/form", form={}, cookie=credentials["browser_secret"])[0], 403)
                self.assertEqual(self.read_owner(new_credentials, new_binding, fixture=local)["sequence"], 0)
            finally:
                server.shutdown()
                thread.join(2)

    def test_concurrent_old_post_and_handoff_leave_atomic_new_baseline(self):
        credentials, binding, _ = self.acquire()
        barrier = threading.Barrier(3)
        results = {}
        errors = []

        def old_post():
            try:
                barrier.wait(timeout=2)
                results["post"] = self.fixture.request("/settings", form={"notify_email": "on"},
                                                       cookie=credentials["browser_secret"])
            except Exception as error:
                errors.append(type(error).__name__)

        def handoff():
            try:
                barrier.wait(timeout=2)
                self.release(credentials, binding)
                results["new"] = self.acquire(self.credentials(attempt="attempt-2"))
            except Exception as error:
                errors.append(type(error).__name__)

        threads = [threading.Thread(target=old_post), threading.Thread(target=handoff)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(6)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        status = results["post"][0]
        self.assertIn(status, (303, 403))
        new_credentials, new_binding, receipt = results["new"]
        baseline = receipt["baseline"]
        final = self.read_owner(new_credentials, new_binding)
        self.assertEqual(final["state"], baseline["state"])
        self.assertEqual(final["sequence"], baseline["sequence"])
        self.assertEqual(final["log"], baseline["log"])
        self.assertEqual(final["effects"], [])
        self.assertEqual(baseline["state"]["settings_saved"], int(status == 303))
        self.assertEqual(len(baseline["log"]), int(status == 303))
        self.assertTrue(all(event["binding"] == binding for event in baseline["log"]))

    def test_foreign_origin_and_wrong_run_are_rejected_before_claim(self):
        credentials = self.credentials()
        status, _ = self.fixture.request("/__claim", payload=credentials,
                                         headers={"Origin": "http://foreign.invalid"})
        self.assertEqual(status, 403)
        bad_run = copy.deepcopy(credentials)
        bad_run["identity"]["run_id"] = "wrong-run"
        self.assertEqual(self.fixture.request("/__claim", payload=bad_run)[0], 409)
        self.assertEqual(self.fixture.request("/__state")[1]["phase"], "unclaimed")

    def test_revoke_before_delayed_claim_body_creates_a_durable_fence(self):
        credentials = self.credentials()
        body = json.dumps(credentials).encode()
        headers = (f"POST /__claim HTTP/1.1\r\nHost: 127.0.0.1:{self.fixture.port}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                   "Connection: close\r\n\r\n").encode()
        with socket.create_connection(("127.0.0.1", self.fixture.port), timeout=2) as sock:
            # The server cannot complete acquisition until the withheld body is
            # delivered. Revoke overtakes this real, already connected request.
            sock.sendall(headers + body[:1])
            status, revoked = self.owner("/__owner/revoke", credentials)
            self.assertEqual(status, 200)
            self.assertEqual(revoked["phase"], "revoked_before_claim")
            self.assertEqual(revoked["identity"], credentials["identity"])
            self.assertEqual(set(revoked), {"phase", "identity", "instance_id"})
            sock.sendall(body[1:])
            with http.client.HTTPResponse(sock) as response:
                response.begin()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.load(response)["error"], "claim_revoked")
        self.assertEqual(self.read_owner(credentials), revoked)
        self.assertEqual(self.fixture.request("/__state")[1]["phase"], "unclaimed")
        self.assertEqual(self.fixture.request("/form", form={}, cookie=credentials["browser_secret"])[0], 403)
        self.assertEqual(self.fixture.request("/__claim", payload=credentials)[0], 409)

    def test_preclaim_revoke_preserves_another_live_owner(self):
        credentials, binding, _ = self.acquire()
        cancelled = self.credentials(attempt="cancelled-before-claim")
        status, fence = self.owner("/__owner/revoke", cancelled)
        self.assertEqual(status, 200)
        self.assertEqual(fence["phase"], "revoked_before_claim")
        current = self.read_owner(credentials, binding)
        self.assertEqual(current["phase"], "active")
        self.assertEqual(current["binding"], binding)
        self.assertEqual(self.fixture.request("/reports", cookie=credentials["browser_secret"])[0], 200)
        self.release(credentials, binding)
        self.assertEqual(self.fixture.request("/__claim", payload=cancelled)[0], 409)
        next_credentials, next_binding, _ = self.acquire(self.credentials(attempt="fresh-attempt"))
        self.assertEqual(self.read_owner(next_credentials, next_binding)["phase"], "active")

    def test_late_export_cannot_cross_release_reassignment_or_baseline(self):
        credentials, binding, receipt = self.acquire()
        responses = []
        errors = []

        def export():
            try:
                responses.append(self.fixture.request("/slow/export", form={},
                                                      cookie=credentials["browser_secret"]))
            except Exception as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=export)
        thread.start()
        try:
            expires = time.monotonic() + 2
            while time.monotonic() < expires:
                current = self.read_owner(credentials, binding)
                if current["pending_effects"]:
                    break
                time.sleep(0.005)
            else:
                self.fail("old export was never observed in flight")
            self.assertEqual(current["pending_effects"], [{"path": "/slow/export", "binding": binding}])
            self.assertEqual(current["state"]["exports"], 0)
            self.assertEqual(current["log"], [])
            self.release(credentials, binding)
            new_credentials, new_binding, new_receipt = self.acquire(self.credentials(attempt="attempt-2"))
            self.assertEqual(new_receipt["baseline"]["state"]["exports"], 0)
            self.assertEqual(new_receipt["baseline"]["sequence"], receipt["baseline"]["sequence"])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(responses[0][0], 409)
            final = self.read_owner(new_credentials, new_binding)
            self.assertEqual(final["state"]["exports"], 0)
            self.assertEqual(final["effects"], [])
            self.assertEqual(final["sequence"], new_receipt["baseline"]["sequence"])
        finally:
            thread.join(5)

    def test_successful_export_effect_and_state_appear_together(self):
        credentials, binding, _ = self.acquire()
        self.assertEqual(self.fixture.request("/slow/export", form={},
                                             cookie=credentials["browser_secret"])[0], 303)
        current = self.read_owner(credentials, binding)
        self.assertEqual(current["state"]["exports"], 1)
        self.assertEqual(current["sequence"], 1)
        self.assertEqual(len(current["effects"]), 1)
        self.assertEqual(current["effects"][0]["binding"], binding)
        self.assertEqual(current["pending_effects"], [])


if __name__ == "__main__":
    unittest.main()
