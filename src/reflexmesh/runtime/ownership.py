"""Private, supervisor-created capability for a scoped fixture attempt claim.

This capability is created before forking. Secrets are ephemeral and never enter
public evidence, event messages, URLs, or exception messages. A worker receipt
can convey a binding hint, but only this process's authenticated HTTP responses
produce server evidence. A separate shared witness retains concrete changes
observed by the certified browser boundary even if its worker is terminated.
Fixture fencing and a declared browser identity do not prove
exclusive control of a live browser, so the full ownership constraint cannot
pass here. This is an in-process trusted-producer boundary, not a Python sandbox.
"""

from __future__ import annotations

import copy
import json
import math
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from reflexmesh.contracts.execution import ExecutionTask

_IDENTITY_FIELDS = frozenset({"task_id", "task_revision", "attempt_id", "run_id", "claim_id"})
_BINDING_FIELDS = _IDENTITY_FIELDS | {"instance_id", "owner_epoch"}
_SESSION_FIELDS = frozenset({"session_id", "profile_id"})
_BASELINE_FIELDS = frozenset({"run_id", "instance_id", "sequence", "state", "log"})
_SNAPSHOT_FIELDS = _BASELINE_FIELDS | {
    "owner_epoch", "phase", "binding", "effects", "pending_effects", "baseline"}
_FIXTURE_PATHS = frozenset({"/", "/profile", "/reports", "/settings", "/form",
                            "/danger", "/danger/delete", "/slow", "/slow/export"})
_BROWSER_VIOLATIONS = frozenset({"browser_session_changed", "browser_profile_changed",
                                  "browser_configuration_changed"})
_WITNESS_BYTES = 16384
_REASONS = frozenset({
    "fixture_busy", "fixture_mismatch", "ownership_unsupported", "ownership_unavailable",
    "ownership_protocol_error", "ownership_authentication_failed", "ownership_conflict",
    "ownership_response_invalid", "ownership_session_mismatch", "ownership_session_conflict",
    "ownership_acquisition_already_attempted", "ownership_operation_already_attempted",
    "ownership_not_claimed", "ownership_not_quarantined", "ownership_cleanup_unconfirmed",
    "ownership_timeout", "ownership_binding_mismatch",
})


class OwnershipError(ValueError):
    """A bounded reason code, never a server body or underlying exception text."""

    def __init__(self, reason: str):
        self.reason = reason if reason in _REASONS else "ownership_protocol_error"
        super().__init__(self.reason)


@dataclass(frozen=True)
class _OwnershipView:
    identity: tuple
    status: str
    reason: str
    observed_at: float | None
    evidence_refs: tuple[str, ...]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not send bearer secrets to a redirect target, even on loopback.
        return None


def _public_id(value: object) -> bool:
    return (type(value) is str and 0 < len(value) <= 256 and value.isprintable() and
            value == value.strip() and value not in (".", "..") and
            not any(char in value for char in ("/", "\\", ":")))


def _finite_number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


class FixtureOwnership:
    """Per-attempt owner capability; only authoritative responses enter its ledger."""

    def __init__(self, ctx, task: ExecutionTask, attempt_id: str):
        if (type(attempt_id) is not str or not attempt_id or
                type(task.revision) is not int or task.revision <= 0):
            raise OwnershipError("ownership_protocol_error")
        self._identity_tuple = (task.task_id, task.revision, attempt_id, task.run_id)
        self._claim_id = str(uuid.uuid4())
        self._origin = task.origin
        self._owner_secret = secrets.token_urlsafe(32)
        self._browser_secret = secrets.token_urlsafe(32)
        self.acquisition_attempted = ctx.Value("i", 0, lock=False)
        self._binding: dict | None = None
        self._verified_binding: dict | None = None
        self._baseline: dict | None = None
        self._phase: str | None = None
        self._binding_uncertain = False
        self._attempted_operations: set[str] = set()
        self._public: list[dict] = []
        self._failure_refs: list[str] = []
        self._failure_time: float | None = None
        self._last_error: str | None = None
        self._gate = None
        # The certified browser is the sole writer. Publish the flag last; no
        # lock or queue feeder can be left held by a terminated worker.
        self._browser_witness = ctx.Array("B", _WITNESS_BYTES, lock=False)
        self._browser_witness_size = ctx.Value("i", 0, lock=False)
        self._browser_witness_time = ctx.Value("d", 0.0, lock=False)
        self._browser_witness_ready = ctx.Value("i", 0, lock=False)

    def __repr__(self) -> str:
        return "FixtureOwnership(<private attempt capability>)"

    @property
    def identity_tuple(self) -> tuple:
        return self._identity_tuple

    @property
    def identity(self) -> dict:
        task_id, revision, attempt_id, run_id = self._identity_tuple
        return {"task_id": task_id, "task_revision": revision, "attempt_id": attempt_id,
                "run_id": run_id, "claim_id": self._claim_id}

    @property
    def binding(self) -> dict | None:
        return copy.deepcopy(self._binding)

    @property
    def verified_binding(self) -> dict | None:
        """This process's last authenticated server binding, never an IPC assertion."""
        return copy.deepcopy(self._verified_binding)

    @property
    def baseline(self) -> dict | None:
        return copy.deepcopy(self._baseline)

    @property
    def browser_secret(self) -> str:
        """Private browser-installation capability; never serialize or trace it."""
        return self._browser_secret

    @property
    def is_claimed(self) -> bool:
        # A local hint only. Consumers needing authority must use read().
        return self._binding is not None

    @property
    def is_active(self) -> bool:
        """Last locally verified phase, not a replacement for a fresh server read."""
        return (self._verified_binding is not None and self._phase == "active" and
                not self._binding_uncertain)

    def _error(self, reason: str):
        self._last_error = reason
        raise OwnershipError(reason) from None

    def _public_identifier(self, value: object) -> bool:
        # A malformed response/SDK must not relabel bearer material as an ID.
        return (_public_id(value) and self._owner_secret not in value and
                self._browser_secret not in value)

    def bind_gate(self, gate) -> None:
        """Bind once to the supervisor's cancellation/claim admission lock."""
        if (self._gate is not None or self.acquisition_attempted.value or
                getattr(gate, "fixture_owner", None) is not self):
            self._error("ownership_protocol_error")
        self._gate = gate

    def _request(self, path: str, payload: dict, timeout: float | None) -> dict:
        if timeout is None:
            timeout = 2.0
        if not _finite_number(timeout) or timeout <= 0:
            self._error("ownership_timeout")
        request = urllib.request.Request(
            self._origin + path, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        # Environment proxy configuration must not receive local bearer secrets.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=min(float(timeout), 2.0)) as response:
                if response.status != 200:
                    self._error("ownership_protocol_error")
                value = json.load(response, object_pairs_hook=_strict_object)
            if type(value) is not dict:
                self._error("ownership_response_invalid")
            return value
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 409:
                reason = "fixture_busy" if path == "/__claim" else "ownership_conflict"
            elif code in (401, 403):
                reason = "ownership_authentication_failed"
            elif code in (404, 405, 501):
                reason = "ownership_unsupported"
            else:
                reason = "ownership_protocol_error"
            self._error(reason)
        except OwnershipError:
            raise
        except (OSError, TimeoutError):
            self._error("ownership_unavailable")
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            self._error("ownership_response_invalid")

    def _validate_binding(self, binding: object) -> dict:
        if (type(binding) is not dict or
                set(binding) not in (_BINDING_FIELDS, _BINDING_FIELDS | _SESSION_FIELDS) or
                type(binding.get("task_revision")) is not int or
                any(binding.get(key) != expected for key, expected in self.identity.items()) or
                not self._public_identifier(binding.get("instance_id")) or
                type(binding.get("owner_epoch")) is not int or binding["owner_epoch"] <= 0 or
                ("session_id" in binding and
                 not all(self._public_identifier(binding.get(key)) for key in _SESSION_FIELDS))):
            self._error("ownership_binding_mismatch")
        return copy.deepcopy(binding)

    def _validate_baseline(self, value: object, binding: dict) -> dict:
        if (type(value) is not dict or set(value) != _BASELINE_FIELDS or
                value.get("run_id") != self.identity["run_id"] or
                value.get("instance_id") != binding["instance_id"] or
                type(value.get("sequence")) is not int or value["sequence"] < 0 or
                type(value.get("state")) is not dict or type(value.get("log")) is not list):
            self._error("ownership_response_invalid")
        return copy.deepcopy(value)

    def _validate(self, value: object, *, acquisition: bool = False) -> tuple[dict, dict]:
        fields = {"binding", "baseline", "phase"} if acquisition else _SNAPSHOT_FIELDS
        if type(value) is not dict or set(value) != fields:
            self._error("ownership_response_invalid")
        binding = self._validate_binding(value["binding"])
        baseline = self._validate_baseline(value["baseline"], binding)
        if value["phase"] not in (("active",) if acquisition else ("active", "quarantined", "released")):
            self._error("ownership_response_invalid")
        # An adopted receipt can pin an instance/epoch but cannot prove a claim.
        if self._binding is not None and any(
                binding[key] != self._binding[key] for key in _BINDING_FIELDS):
            self._error("ownership_binding_mismatch")
        if self._baseline is not None and baseline != self._baseline:
            self._error("ownership_response_invalid")
        if not acquisition:
            if (value["run_id"] != binding["run_id"] or
                    value["instance_id"] != binding["instance_id"] or
                    type(value["owner_epoch"]) is not int or
                    value["owner_epoch"] != binding["owner_epoch"] or
                    type(value["sequence"]) is not int or value["sequence"] < baseline["sequence"] or
                    type(value["state"]) is not dict or type(value["log"]) is not list or
                    type(value["effects"]) is not list or type(value["pending_effects"]) is not list):
                self._error("ownership_response_invalid")
        return binding, baseline

    def adopt(self, receipt: dict) -> None:
        """Accept a worker's binding hint, never its baseline or assertion as proof."""
        if type(receipt) is not dict:
            self._error("ownership_response_invalid")
        binding, _baseline = self._validate(receipt, acquisition=set(receipt) == {
            "binding", "baseline", "phase"})
        # Do not let late worker messages erase a server-confirmed session.
        if self._verified_binding is None:
            self._binding = binding

    def _auth_payload(self, *, recovery: bool) -> dict:
        # A basic or transported binding may have gained a session in the worker;
        # identity recovery authenticates the same claim without trusting IPC.
        if recovery and (self._verified_binding is None or self._binding_uncertain or
                         "session_id" not in self._verified_binding):
            return {"owner_secret": self._owner_secret, "identity": self.identity}
        if self._binding is None:
            self._error("ownership_not_claimed")
        return {"owner_secret": self._owner_secret, "binding": self.binding}

    def _safe_effects(self, values: list, binding: dict, baseline: dict, *, pending: bool) -> list:
        """Only server-owned, allowlisted metadata; never raw log/state/data."""
        result = []
        for event in values:
            fields = {"path", "binding"} if pending else {"seq", "t", "method", "path", "binding"}
            if (type(event) is not dict or set(event) != fields or
                    type(event.get("path")) is not str or event["path"] not in _FIXTURE_PATHS or
                    type(event.get("binding")) is not dict or event["binding"] != binding or
                    type(event["binding"].get("task_revision")) is not int or
                    type(event["binding"].get("owner_epoch")) is not int):
                continue
            if pending:
                result.append({"path": event["path"], "binding": copy.deepcopy(binding)})
            elif (type(event.get("seq")) is int and event["seq"] > baseline["sequence"] and
                  event.get("method") == "POST" and _finite_number(event.get("t"))):
                result.append({"sequence": event["seq"], "observed_at": event["t"],
                               "method": "POST", "path": event["path"],
                               "binding": copy.deepcopy(binding)})
        return result

    def _record(self, operation: str, request: dict, value: dict,
                binding: dict, baseline: dict, *, conflict: bool = False) -> str:
        reference = f"runtime:{self._identity_tuple[2]}:ownership:{len(self._public) + 1}"
        record = {"ref": reference, "version": 1, **self.identity,
                  "kind": "fixture_owner_response", "source": "authenticated_fixture_response",
                  "capture_storage": "attempt_memory", "operation": operation,
                  "scope": "fixture_claim_and_declared_browser_identity", "http_status": 200,
                  "status": "fail" if conflict else "unknown", "observed_at": time.time(),
                  "request_identity": self.identity, "request_binding": copy.deepcopy(request.get("binding")),
                  "response_binding": copy.deepcopy(binding), "phase": value["phase"],
                  "atomic_baseline_seq": baseline["sequence"],
                  "sequence": value.get("sequence", baseline["sequence"]),
                  "effects": self._safe_effects(value.get("effects", []), binding, baseline, pending=False),
                  "pending_effects": self._safe_effects(value.get("pending_effects", []), binding,
                                                        baseline, pending=True)}
        record["source_ref"] = (f"fixture:{binding['run_id']}:{binding['instance_id']}:"
                                f"{binding['claim_id']}:{binding['owner_epoch']}:{record['sequence']}")
        if conflict:
            record["previous_response_binding"] = copy.deepcopy(self._verified_binding)
        self._public.append(record)
        return reference

    def _accept(self, operation: str, request: dict, value: dict, *, acquisition: bool = False) -> dict:
        if type(value) is dict and value.get("phase") == "revoked_before_claim":
            return self._accept_preclaim_fence(operation, request, value)
        binding, baseline = self._validate(value, acquisition=acquisition)
        conflict = (self._verified_binding is not None and "session_id" in self._verified_binding and
                    any(binding.get(key) != self._verified_binding[key] for key in _SESSION_FIELDS))
        reference = self._record(operation, request, value, binding, baseline, conflict=conflict)
        if conflict:
            self._failure_refs.append(reference)
            self._failure_time = time.time()
            self._error("ownership_session_conflict")
        self._binding = binding
        self._verified_binding = copy.deepcopy(binding)
        self._baseline = baseline
        self._phase = value["phase"]
        self._binding_uncertain = False
        self._last_error = None
        return copy.deepcopy(value)

    def _accept_preclaim_fence(self, operation: str, request: dict, value: dict) -> dict:
        if (operation not in ("read", "revoke") or "identity" not in request or
                set(value) != {"phase", "identity", "instance_id"} or
                type(value.get("identity")) is not dict or value["identity"] != self.identity or
                type(value["identity"].get("task_revision")) is not int or
                not self._public_identifier(value.get("instance_id"))):
            self._error("ownership_response_invalid")
        reference = f"runtime:{self._identity_tuple[2]}:ownership:{len(self._public) + 1}"
        self._public.append({"ref": reference, "version": 1, **self.identity,
                             "kind": "fixture_preclaim_fence", "source": "authenticated_fixture_response",
                             "capture_storage": "attempt_memory", "operation": operation,
                             "scope": "fixture_claim_fence", "http_status": 200, "status": "unknown",
                             "observed_at": time.time(), "request_identity": self.identity,
                             "response_identity": copy.deepcopy(value["identity"]),
                             "instance_id": value["instance_id"], "phase": "revoked_before_claim"})
        self._phase = "revoked_before_claim"
        self._last_error = None
        return copy.deepcopy(value)

    def _record_browser_violation(self, reason: str, *, session_id: str | None = None,
                                  profile_id: str | None = None) -> bool:
        """Private certified-producer hook for an observed concrete identity change.

        FixtureBrowser calls this only after its real session/profile and cookie
        were bound, and only after comparing actual boundary objects/configuration.
        Missing information, caller arguments, and worker event assertions do not
        call this hook. Values such as filesystem paths and CDP URLs are omitted.
        Like the immutable-slot sink, this is not a sandbox against Python code
        that replaces a trusted producer or accesses its private capabilities.
        """
        if (type(reason) is not str or reason not in _BROWSER_VIOLATIONS or
                self._verified_binding is None or "session_id" not in self._verified_binding or
                self._phase != "active" or self._binding_uncertain or self._baseline is None):
            return False
        if self._browser_witness_ready.value:
            return True
        value = {"binding": self.verified_binding, "reason": reason,
                 "session_id": session_id if self._public_identifier(session_id) else None,
                 "profile_id": profile_id if self._public_identifier(profile_id) else None}
        packet = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(packet) > _WITNESS_BYTES:
            return False
        self._browser_witness[:len(packet)] = packet
        self._browser_witness_size.value = len(packet)
        self._browser_witness_time.value = time.time()
        self._browser_witness_ready.value = 1
        return True

    def _retain_browser_witness(self) -> str | None:
        if not self._browser_witness_ready.value:
            return None
        reference = f"runtime:{self._identity_tuple[2]}:ownership:browser-boundary"
        if any(record["ref"] == reference for record in self._public):
            return reference
        size = self._browser_witness_size.value
        if not 0 < size <= _WITNESS_BYTES:
            return None
        try:
            value = json.loads(bytes(self._browser_witness[:size]), object_pairs_hook=_strict_object)
            if (type(value) is not dict or set(value) != {"binding", "reason", "session_id", "profile_id"} or
                    value["reason"] not in _BROWSER_VIOLATIONS or
                    type(value["binding"]) is not dict or
                    set(value["binding"]) != _BINDING_FIELDS | _SESSION_FIELDS or
                    type(value["binding"].get("task_revision")) is not int or
                    any(value["binding"].get(key) != expected for key, expected in self.identity.items()) or
                    not self._public_identifier(value["binding"].get("instance_id")) or
                    type(value["binding"].get("owner_epoch")) is not int or value["binding"]["owner_epoch"] <= 0 or
                    any(not self._public_identifier(value["binding"].get(key)) for key in _SESSION_FIELDS) or
                    any(value[key] is not None and not self._public_identifier(value[key]) for key in _SESSION_FIELDS)):
                return None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return None
        self._public.append({"ref": reference, "version": 1, **self.identity,
                             "kind": "browser_identity_mismatch", "source": "attempt_memory",
                             "capture_storage": "attempt_memory", "status": "fail",
                             "scope": "controlled_fixture_private_profile",
                             "boundary": "fixture_browser.private_session_guard/v1",
                             "observed_at": self._browser_witness_time.value,
                             "binding": value["binding"], "reason": value["reason"],
                             "observed_session_id": value["session_id"],
                             "observed_profile_id": value["profile_id"]})
        return reference

    def acquire(self, timeout: float | None = 2.0) -> dict:
        if self._gate is None:
            if self.acquisition_attempted.value:
                self._error("ownership_acquisition_already_attempted")
            self.acquisition_attempted.value = 1
        else:
            # Cancellation before this critical section prevents acquisition;
            # cancellation after it must observe the shared attempted marker.
            # No network, browser, IPC, or evidence operation occurs under lock.
            self._gate._acquire()
            try:
                self._gate._check()
                if self.acquisition_attempted.value:
                    self._error("ownership_acquisition_already_attempted")
                self.acquisition_attempted.value = 1
            finally:
                self._gate.lock.release()
        # The marker survives worker termination and a lost acquisition receipt.
        request = {"identity": self.identity, "owner_secret": self._owner_secret,
                   "browser_secret": self._browser_secret}
        return self._accept("acquire", request, self._request("/__claim", request, timeout), acquisition=True)

    def bind_session(self, session_id: str, profile_id: str, timeout: float | None = 2.0) -> dict:
        if not self._public_identifier(session_id) or not self._public_identifier(profile_id):
            self._error("ownership_session_mismatch")
        if self._verified_binding is None or self._phase != "active":
            self._error("ownership_not_claimed")
        if "bind" in self._attempted_operations or "session_id" in self._verified_binding:
            self._error("ownership_operation_already_attempted")
        request = {**self._auth_payload(recovery=False), "session_id": session_id, "profile_id": profile_id}
        self._attempted_operations.add("bind")
        self._binding_uncertain = True
        value = self._request("/__owner/bind", request, timeout)
        binding, _baseline = self._validate(value)
        if binding.get("session_id") != session_id or binding.get("profile_id") != profile_id:
            self._error("ownership_session_mismatch")
        if value["phase"] != "active":
            self._error("ownership_response_invalid")
        return self._accept("bind", request, value)

    def assert_session(self, session_id: str, profile_id: str) -> None:
        """Local pinned-identity check, not a live browser exclusivity assertion."""
        if (self._binding is None or self._verified_binding is None or self._binding_uncertain or
                self._phase != "active" or not self._public_identifier(session_id) or
                not self._public_identifier(profile_id) or
                self._binding.get("session_id") != session_id or
                self._binding.get("profile_id") != profile_id):
            self._error("ownership_session_mismatch")

    def read(self, timeout: float | None = 2.0) -> dict:
        request = self._auth_payload(recovery=True)
        return self._accept("read", request, self._request("/__owner/state", request, timeout))

    def revoke(self, timeout: float | None = 2.0) -> dict:
        if "revoke" in self._attempted_operations:
            self._error("ownership_operation_already_attempted")
        request = self._auth_payload(recovery=True)
        self._attempted_operations.add("revoke")
        value = self._request("/__owner/revoke", request, timeout)
        if type(value) is not dict or value.get("phase") not in ("quarantined", "revoked_before_claim"):
            self._error("ownership_response_invalid")
        return self._accept("revoke", request, value)

    def release(self, cleanup: str, timeout: float | None = 2.0) -> dict:
        if cleanup not in ("closed", "forced"):
            self._error("ownership_cleanup_unconfirmed")
        if self._verified_binding is None or self._phase != "quarantined":
            self._error("ownership_not_quarantined")
        if "release" in self._attempted_operations:
            self._error("ownership_operation_already_attempted")
        request = {**self._auth_payload(recovery=False), "cleanup_confirmed": True}
        self._attempted_operations.add("release")
        value = self._request("/__owner/release", request, timeout)
        if type(value) is not dict or value.get("phase") != "released":
            self._error("ownership_response_invalid")
        return self._accept("release", request, value)

    def view(self) -> _OwnershipView:
        witness = self._retain_browser_witness()
        if witness is not None:
            return _OwnershipView(self._identity_tuple, "fail",
                                  "The controlled fixture browser's bound session, profile, or configuration changed.",
                                  self._browser_witness_time.value, (witness,) + tuple(self._failure_refs))
        if self._failure_refs:
            return _OwnershipView(self._identity_tuple, "fail",
                                  "Authenticated fixture responses changed this claim's bound browser identity.",
                                  self._failure_time, tuple(self._failure_refs))
        if self._last_error:
            reason = f"Fixture ownership evidence is unavailable ({self._last_error}); browser-controller exclusivity is unavailable."
        elif self._phase == "revoked_before_claim":
            reason = "The fixture fenced this attempt before acquisition; browser-controller exclusivity is unavailable."
        elif self._verified_binding is not None and "session_id" in self._verified_binding:
            reason = "Scoped fixture claim and declared session binding verified; browser-controller exclusivity is unavailable."
        elif self._verified_binding is not None:
            reason = "Scoped fixture claim verified; browser session binding and browser-controller exclusivity are unavailable."
        elif self._binding is not None:
            reason = "Worker binding awaits an authenticated fixture response; browser-controller exclusivity is unavailable."
        else:
            reason = "An authenticated fixture claim and browser-controller exclusivity are unavailable."
        return _OwnershipView(self._identity_tuple, "unknown", reason, None, ())

    def export(self) -> list[dict]:
        """Retained redacted source records, independent of the unknown full constraint."""
        self._retain_browser_witness()
        return copy.deepcopy(self._public)
