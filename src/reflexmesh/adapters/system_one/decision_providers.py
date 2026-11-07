"""Decision providers for the generic browser path (V0.6 sections 7 and 9).

`script` is a deterministic stand-in that picks among the options a step offers by their
visible description. It proves the mechanics of the loop and says nothing about Jev.
`jev` is the real provider, built from the environment with provider retries OFF: a
hidden retry would be a model request the call budget never saw.
`chat` is an ordinary LLM over the same options, the comparison of section 17.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from urllib.parse import urlsplit

CONTROL_ACTIONS = ("finish", "escalate")


class ProviderUnavailable(RuntimeError):
    """No provider key or an unusable configuration: the run is `blocked/executor_unavailable`."""


class ScriptMismatch(RuntimeError):
    """A scripted step matched no offered option, or more than one."""


def _is_probability(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0.0 <= value <= 1.0


class LabelScriptProvider:
    """Plays a fixed list of steps against whatever options each step offers.

    A step is `{"action": "click", "element": "Search"}`; every other key is the name of a
    parameter and a regular expression that must match exactly one offered option
    description. Option references are never written in a script: they are generated per
    observation, which is the point of a site with no prepared scenario.
    """

    model = "script/label"

    def __init__(self, steps: list[dict]):
        self.steps = [dict(step) for step in steps]
        self.calls = 0

    def decide(self, state, questions: dict):
        from systemone_harness.provider import Decision, ProviderError

        step = self.steps[self.calls] if self.calls < len(self.steps) else {"action": "finish"}
        self.calls += 1
        try:
            answers = answers_for_step(step, questions)
        except ScriptMismatch as exc:
            raise ProviderError(f"script: {exc} at step {self.calls}") from None
        return Decision(answers=answers, usage={"input_tokens": 0, "output_tokens": 0}, model=self.model,
                        request_id=f"script-{self.calls}", latency_ms=1, raw={"script_step": self.calls})

    def close(self):
        pass


def _choice(criteria: dict, pick: str) -> dict:
    return {"type": "choice", "choice": pick, "confidence": 0.99,
            "probabilities": {name: (0.99 if name == pick else 0.0) for name in criteria}}


def answers_for_step(step: dict, questions: dict) -> dict:
    """Decision answers (wire shape) for one scripted step against the options a step offers.

    Raises ScriptMismatch when the action is not offered or a pattern matches no option, or
    more than one. Shared by the script provider and the test stand-in for the decisions endpoint.
    """
    action = step.get("action")
    offered = (questions.get("next_action") or {}).get("criteria") or {}
    if action not in offered:
        raise ScriptMismatch(f"action {action!r} is not offered")
    answers = {"next_action": _choice(offered, action),
               "goal_reached": {"type": "noul", "noul": 0.95 if action == "finish" else 0.0}}
    for key, question in questions.items():
        if key in answers:
            continue
        if question.get("type") == "noul":
            answers[key] = {"type": "noul", "noul": 0.99}
            continue
        if question.get("type") != "choice":
            answers[key] = {"type": "score", "score": 0, "probabilities": {}, "confidence": 0.99}
            continue
        criteria = question.get("criteria") or {}
        parameter = key.split("__", 1)[1] if "__" in key else key
        if key.startswith(f"{action}__") and parameter in step:
            pattern = re.compile(str(step[parameter]), re.IGNORECASE)
            matches = [ref for ref, description in criteria.items() if pattern.search(str(description))]
            if len(matches) != 1:
                raise ScriptMismatch(f"{parameter!r} matched {len(matches)} options")
            answers[key] = _choice(criteria, matches[0])
        elif criteria:
            answers[key] = _choice(criteria, next(iter(criteria)))
    return answers


class ValidatingProvider:
    """Rejects any answer that names something the step did not offer (`failed/protocol_error`).

    SOH's gate already refuses an unknown action; it does not check that a chosen target or
    slot belongs to the offered set, and it trusts finite probabilities. Here an unknown
    operation, target or parameter is a protocol error, never a guess.
    """

    def __init__(self, inner):
        self.inner = inner
        self.last_decision = None

    @property
    def model(self):
        return getattr(self.inner, "model", None)

    @property
    def _client(self):
        return self.inner._client  # AttributeError when absent keeps CountingProvider's hasattr honest

    def decide(self, state, questions: dict):
        decision = self.inner.decide(state, questions)
        try:
            self._validate(decision.answers, questions)
        except Exception as exc:
            shape = (decision.raw or {}).get("reply_shape") if isinstance(decision.raw, dict) else None
            if shape is not None:
                exc.reply_shape = shape
            # A refused reply was still a paid request: its id and usage go with the refusal, or its cost is lost.
            exc.request_id, exc.usage = getattr(decision, "request_id", None), getattr(decision, "usage", None)
            raise
        self.last_decision = decision
        return decision

    @staticmethod
    def _validate(answers, questions: dict) -> None:
        from systemone_harness.provider import ProviderError

        def bad(reason):
            raise ProviderError(f"protocol: {reason}")

        if type(answers) is not dict:
            bad("answers is not an object")
        for key, answer in answers.items():
            if type(answer) is not dict:
                bad(f"answer {key!r} is not an object")
            for field in ("confidence", "noul"):
                if answer.get(field) is not None and not _is_probability(answer[field]):
                    bad(f"{field} of {key!r} is not a probability")
            probabilities = answer.get("probabilities") or {}
            if type(probabilities) is not dict or any(not _is_probability(p) for p in probabilities.values()):
                bad(f"probabilities of {key!r} are not probabilities")
        chosen = (answers.get("next_action") or {}).get("choice")
        offered = (questions.get("next_action") or {}).get("criteria") or {}
        if type(chosen) is not str or chosen not in offered:
            bad("next_action names an operation that was not offered")
        if chosen in CONTROL_ACTIONS:
            return
        for key, question in questions.items():
            if not key.startswith(f"{chosen}__") or key.endswith("__stated") or question.get("type") != "choice":
                continue
            answer = answers.get(key)
            value = answer.get("choice") if type(answer) is dict else None
            if type(value) is not str or value not in (question.get("criteria") or {}):
                bad(f"{key} names a target or value that was not offered")

    def close(self):
        if hasattr(self.inner, "close"):
            self.inner.close()


def json_objects(text: str) -> list[dict]:
    """The top-level JSON objects in a reply. `response_format` is not honoured on every route: a model may fence
    the object or write a sentence around it. That is formatting; two objects are a choice not made, and not guessed."""
    decoder, found, at = json.JSONDecoder(), [], 0
    while (at := text.find("{", at)) != -1:
        try:
            value, end = decoder.raw_decode(text, at)
        except ValueError:
            at += 1
            continue
        found.append(value)
        at = end
    return found


class ChatClient:
    """One JSON reply from an ordinary chat model through OpenRouter; no retries, for the same reason as Jev."""

    def __init__(self, api_key: str, model: str, timeout: float = 60.0, base_url: str = "https://openrouter.ai"):
        import httpx

        self.model, self.base_url = model, base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        self._client = httpx.Client(timeout=timeout)

    def ask(self, system: str, content: object) -> tuple[dict, dict, str, str, int]:
        """(reply object, usage, served model, request id, latency in ms)."""
        import httpx
        from systemone_harness.provider import ProviderError

        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "usage": {"include": True},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(content, ensure_ascii=False)}]}
        started = time.time()
        try:
            r = self._client.post(self.base_url + "/api/v1/chat/completions", json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderError(type(exc).__name__) from None
        latency = int((time.time() - started) * 1000)
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code} from the provider", r.status_code)
        try:
            data = r.json()
        except ValueError:
            data = None
        data = data if isinstance(data, dict) else {}
        used = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = {"input_tokens": int(used.get("prompt_tokens") or 0), "output_tokens": int(used.get("completion_tokens") or 0)}
        if type(used.get("cost")) in (int, float):
            usage["cost"] = used["cost"]
        request_id = str(data.get("id") or "")

        def refused(message: str) -> Exception:
            # The reply is refused, the request was paid for: its id and usage go with the refusal (review of PR #15).
            exc = ProviderError(message)
            exc.request_id, exc.usage = request_id or None, usage
            return exc

        try:
            objects = json_objects(data["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError):
            raise refused("protocol: the model did not answer with a JSON object") from None
        if len(objects) != 1:
            raise refused(f"protocol: the model answered with {len(objects)} JSON objects, not one")
        return objects[0], usage, str(data.get("model") or self.model), request_id, latency

    def close(self) -> None:
        self._client.close()


CHAT_SYSTEM = ("You operate a web page toward the goal in `state`, one action at a time. Choose only from what `questions` "
               "offers, by its key, and never invent one. A value can be typed only by choosing its key. Finish when the "
               "page shows the goal is done. Escalate when nothing offered can move it forward. Reply with one JSON object: "
               "`next_action`, one key per parameter of that action named `<action>__<parameter>`, and `goal_reached`, a "
               "number from 0 to 1.")


class ChatChoiceProvider:
    """Mode L of section 17: an ordinary LLM chooses every action from the same finite options, through the same
    adapter, gate, verifier and budget. It states no calibrated confidence, so its choice passes the threshold;
    every other check of the gate still applies."""

    def __init__(self, client: ChatClient):
        self.client = client
        self.model = client.model

    @property
    def _client(self):
        return self.client._client

    def decide(self, state, questions: dict):
        from systemone_harness.provider import Decision

        reply, usage, model, request_id, latency = self.client.ask(CHAT_SYSTEM, {"state": state, "questions": questions})
        action = reply.get("next_action")
        goal = reply.get("goal_reached")
        offered = (questions.get("next_action") or {}).get("criteria") or {}
        answers = {"next_action": {"type": "choice", "choice": action, "confidence": 0.99, "probabilities": {}},
                   "goal_reached": {"type": "noul", "noul": 0.95 if action == "finish" else
                                    (float(goal) if _is_probability(goal) else 0.0)}}
        if type(action) is str and action in offered:
            answers["next_action"]["probabilities"] = {name: (0.99 if name == action else 0.0) for name in offered}
        for key, question in questions.items():
            if key in answers:
                continue
            if question.get("type") == "noul":
                answers[key] = {"type": "noul", "noul": 0.99}
            elif question.get("type") != "choice":
                answers[key] = {"type": "score", "score": 0, "probabilities": {}, "confidence": 0.99}
            elif key.startswith(f"{action}__"):
                pick = reply.get(key)   # a wrong one is refused by ValidatingProvider, never guessed
                if type(pick) is int:   # JSON has no way to insist: the same key, written as a number
                    pick = str(pick)
                answers[key] = {"type": "choice", "choice": pick, "confidence": 0.99, "probabilities": {}}
            elif question.get("criteria"):
                answers[key] = _choice(question["criteria"], next(iter(question["criteria"])))
        shape = {str(k)[:40]: (v if k == "next_action" and type(v) is str and len(v) <= 40 else type(v).__name__)
                 for k, v in list(reply.items())[:12]}   # for a refused reply: its form, never a value
        return Decision(answers=answers, usage=usage, model=model, request_id=request_id, latency_ms=latency,
                        raw={"reply_shape": shape})

    def close(self):
        self.client.close()


def chat_provider_from_env(model: str | None, timeout: float = 60.0, base_url: str | None = None):
    """The model is always named: there is no default LLM to compare against."""
    if not model:
        raise ProviderUnavailable("the chat provider requires --model")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ProviderUnavailable("no key for the chat provider: set OPENROUTER_API_KEY")
    client = ChatClient(os.environ["OPENROUTER_API_KEY"], model, timeout,
                        _checked_endpoint(base_url) if base_url else "https://openrouter.ai")
    return ChatChoiceProvider(client), f"openrouter-chat:{model}"


def _checked_endpoint(url: str) -> str:
    """A decisions endpoint that will receive the API key: https, or plain http on loopback only."""
    parts = urlsplit(url)
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or (parts.scheme == "http" and not loopback)):
        raise ProviderUnavailable("the decisions endpoint must be https, or http on loopback, with no path")
    return f"{parts.scheme}://{parts.netloc}"


def jev_provider_from_env(model: str | None = None, timeout: float = 30.0, base_url: str | None = None):
    """OpenRouter (OPENROUTER_API_KEY) or TypeSafe (TYPESAFE_API_KEY); retries are off.

    Returns the provider and a configuration identifier for the result. The key is read
    from the environment only; it is never an argument, a task field or a log line.
    `base_url` is service configuration for a stand-in or proxy; it is checked because the
    key is sent there.
    """
    try:
        from systemone_harness.provider import OpenRouterProvider, TypeSafeProvider
    except ImportError as exc:
        raise ProviderUnavailable("systemone_harness is not installed") from exc
    if os.environ.get("OPENROUTER_API_KEY"):
        name = model or os.environ.get("S1_MODEL", "~typesafe/jev-latest")
        provider = OpenRouterProvider(os.environ["OPENROUTER_API_KEY"], name, timeout=timeout, retries=0)
        config = f"openrouter:{name}"
    elif os.environ.get("TYPESAFE_API_KEY"):
        name = model or os.environ.get("S1_MODEL", "jev-latest")
        provider = TypeSafeProvider(os.environ["TYPESAFE_API_KEY"], name, timeout=timeout, retries=0)
        config = f"typesafe:{name}"
    else:
        raise ProviderUnavailable("no Jev provider key: set OPENROUTER_API_KEY or TYPESAFE_API_KEY")
    if base_url:
        provider.base_url = _checked_endpoint(base_url)
        config += f"@{urlsplit(provider.base_url).netloc}"
    return provider, config
