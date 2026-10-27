"""Registered checks (V0.6 section 11, `registered_check`): trusted sources the service, not the task, names.

The service reads a checks file (`reflexmesh browser run --checks FILE`):

    {"checks": {"order_desk.note_saved_once": {"kind": "order_desk", "url": "http://127.0.0.1:8770"}}}

A task can only refer to a `check_id` listed there; it cannot supply a URL, code or a kind. Each kind is code in
this module with its own strict arguments and its own way of binding evidence to the attempt.

`order_desk` is the oracle of the demo order desk (`experiments/v06/site/server.py`). It is a test oracle for that
one site, not a way to verify arbitrary sites. Before the first navigation it claims the site for the attempt
and gets a secret token; the site tags every request with the active claim and refuses a second claim. After
the browser has stopped it reads, with the token, what happened under the claim: the requests, the notes added,
and how many requests were still being handled. From that it judges the criterion ("this order gained exactly
this note, once") and settles the attempt's actions: a click that submits a POST form is `applied` when the site
recorded as many POSTs to that path as such clicks were committed; every other action is `not_applied`, because
this site changes state only on POST. It also judges repeats: more POSTs than committed submits is a confirmed
repeat, even one made inside the driver.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from reflexmesh.contracts.browser import canonical_origin
from reflexmesh.verification.page_verifier import normalized


class CheckUnavailable(RuntimeError):
    """A bounded reason; never a server body or the token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a bearer token never follows a redirect


_OPENER = urllib.request.build_opener(_NoRedirect)


class OrderDeskCheck:
    kind = "order_desk"

    def __init__(self, check_id: str, url: str):
        origin = canonical_origin(url)
        host = urlsplit(url).hostname or ""
        if origin is None or not (origin.startswith("https://") or host in ("127.0.0.1", "localhost", "::1")):
            raise ValueError("an order_desk check needs https or a loopback http origin")
        self.check_id, self.origin = check_id, origin
        self._token: str | None = None
        self.claim_id: str | None = None
        self.attempt_id: str | None = None

    def __repr__(self) -> str:  # the token never appears in a repr, a log or an exception
        return f"OrderDeskCheck({self.check_id!r}, {self.origin!r}, claimed={self.claim_id is not None})"

    def _call(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.origin + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(request, timeout=max(0.1, timeout)) as response:
                value = json.loads(response.read(1 << 20))
        except urllib.error.HTTPError as exc:
            raise CheckUnavailable("check_busy" if exc.code == 409 else "check_refused") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise CheckUnavailable("check_unreachable") from None
        if type(value) is not dict:
            raise CheckUnavailable("check_response_invalid")
        return value

    def claim(self, attempt_id: str, timeout: float = 2.0) -> None:
        answer = self._call("POST", "/__claim", {"attempt_id": attempt_id}, timeout)
        if (answer.get("attempt_id") != attempt_id or type(answer.get("claim_id")) is not str or
                type(answer.get("token")) is not str or not answer["token"]):
            raise CheckUnavailable("check_response_invalid")
        self._token, self.claim_id, self.attempt_id = answer["token"], answer["claim_id"], attempt_id

    def evidence(self, timeout: float = 2.0) -> dict:
        if not self._token:
            raise CheckUnavailable("check_not_claimed")
        value = self._call("GET", "/__evidence", None, timeout)
        if (value.get("claim_id") != self.claim_id or value.get("attempt_id") != self.attempt_id or
                type(value.get("requests")) is not list or type(value.get("notes_added")) is not dict or
                type(value.get("pending")) is not int):
            raise CheckUnavailable("check_binding_mismatch")
        return value

    def release(self, timeout: float = 2.0) -> None:
        if self._token:
            try:
                self._call("POST", "/__release", {}, timeout)
            finally:
                self._token = None

    # ── judgements ──
    def judge(self, args: dict, expected: list[str], evidence: dict | None) -> dict:
        """`subject` is the order id; `expected_slot_refs` hold the one note it must have gained, once."""
        if evidence is None:
            return {"status": "unknown", "reason": "check_unavailable", "facts": {}}
        if evidence["pending"]:
            return {"status": "unknown", "reason": "requests_pending", "facts": {"pending": evidence["pending"]}}
        added = [normalized(n) for n in evidence["notes_added"].get(str(args.get("subject")), [])]
        want = [normalized(v) for v in expected]
        facts = {"added": len(added), "matches": sum(1 for n in added if n in want)}
        if len(want) == 1 and added == want:
            return {"status": "pass", "reason": "saved_once", "facts": facts}
        return {"status": "fail", "reason": "not_saved" if not added else "saved_otherwise", "facts": facts}

    def settle(self, actions: list[dict], evidence: dict, *, browser_stopped: bool) -> tuple[str, str] | None:
        """Settle `unknown` effects in place; return the repeat verdict, or None when it cannot be decided."""
        if evidence["pending"]:
            return None
        posts: dict[str, int] = {}
        for request in evidence["requests"]:
            if request.get("method") == "POST":
                posts[request["path"]] = posts.get(request["path"], 0) + 1
        submits: dict[str, list[dict]] = {}
        for row in actions:
            request = row.get("request") or {}
            if row.get("operation") == "click" and request.get("method") == "POST":
                submits.setdefault(request.get("path"), []).append(row)
        ref = f"check:{self.check_id}:{self.claim_id}"
        if any(posts.get(path, 0) > len(rows) for path, rows in submits.items()):
            return "fail", "The site recorded more POSTs to a form than submits were committed for it."
        if any(path not in submits for path in posts):
            return None  # a POST no committed submit accounts for: not a proof either way
        accounted = all(posts.get(path, 0) == len(rows) for path, rows in submits.items())
        for path, rows in submits.items():
            for row in rows:
                if row["effect"] != "unknown":
                    continue
                if posts.get(path, 0) == len(rows):
                    row.update(effect="applied", evidence_refs=[ref])
                elif posts.get(path, 0) == 0 and browser_stopped:
                    row.update(effect="not_applied", evidence_refs=[ref])
        if accounted and browser_stopped:
            # Every POST is matched to a committed submit and nothing is pending; on this site nothing else
            # changes state, so the remaining actions had no server effect.
            for row in actions:
                if row["effect"] == "unknown" and row.get("operation") != "navigate":
                    row.update(effect="not_applied", evidence_refs=[ref])
            return "pass", ("The registered order-desk record shows each committed form submit requested exactly "
                            "once, none pending, and no other state change.")
        return None


KINDS = {"order_desk": OrderDeskCheck}


def load_checks(path: str | Path) -> dict:
    """check_id -> check instance, from the service's checks file. Unknown kinds or fields are refused."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if type(data) is not dict or set(data) != {"checks"} or type(data["checks"]) is not dict:
        raise ValueError("a checks file is {\"checks\": {check_id: {...}}}")
    checks = {}
    for check_id, spec in data["checks"].items():
        if type(check_id) is not str or not check_id or type(spec) is not dict or spec.get("kind") not in KINDS:
            raise ValueError("invalid check")
        if set(spec) != {"kind", "url"} or type(spec["url"]) is not str:
            raise ValueError("invalid check arguments")
        checks[check_id] = KINDS[spec["kind"]](check_id, spec["url"])
    return checks
