"""Invoke an adapter for one test case, with a per-case timeout and full
exception capture. Nothing an adapter does -- raise, hang, call sys.exit,
return garbage -- escapes `invoke`; it comes back as a CaseOutcome with
status "error" or "timeout", and the run moves on to the next case.

Adapters are trusted local code (see agentforge_sdk.adapter): this is fault
*containment* for bugs, not a security boundary.

Timeout caveat, stated plainly: a synchronous Python adapter runs in its own
daemon thread, and Python can't kill a thread. On timeout the case is
recorded and the run continues immediately, but the stuck call keeps running
in the background until it returns (or the worker process exits). Each case
gets a fresh thread, so one hung case never delays the next case's timer.
An async adapter is cancelled properly on timeout -- unless it blocks the
event loop with synchronous code, which nothing can interrupt.

A case's `scenario` (mock tool failures, injected tool output, the session
user; agentforge_core.scenario) is delivered as the `scenario` keyword
argument to a python adapter that declares one, and as a "scenario" field
in an http adapter's request body. A python adapter that doesn't declare it
can't run such a case: the case is recorded as an error rather than run
without its setup, which would quietly test something else.

Replay overrides (agentforge_sdk.replay): a python adapter that declared
replay settings gets a replay's validated overrides as the `overrides`
keyword argument -- only when there are some, so a replay without overrides
calls the adapter exactly as a run does. `check_overrides` validates them
against the declaration first; an adapter that declared nothing (and every
http adapter) can't be given any.

Trace context: a sync python adapter's daemon thread runs in a copy of the
caller's context, so spans the agent opens (agentforge_sdk.tracing) are
children of the worker's `invoke_agent` span; an http adapter receives a W3C
`traceparent` header (and `tracestate`, if any) for that span.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import dataclasses
import importlib
import inspect
import json
import math
import re
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from opentelemetry import propagate

from agentforge_sdk import AdapterOutput, Step
from agentforge_sdk.replay import ReplaySettings, replay_settings, validate_overrides

_MAX_ERROR_CHARS = 4000


class AdapterLoadError(Exception):
    """The adapter itself can't be set up (bad import path, not callable)."""


class AdapterOutputError(Exception):
    """The adapter returned something that isn't a valid AdapterOutput."""


class ScenarioNotSupportedError(Exception):
    """The case has a scenario, but the python adapter takes no `scenario` argument."""


@dataclass
class CaseOutcome:
    status: str  # "ok" | "error" | "timeout"
    latency_ms: float
    output: AdapterOutput | None = None
    error_type: str | None = None
    error_message: str | None = None


def coerce_output(raw: Any) -> AdapterOutput:
    """Accept an AdapterOutput, a dict with its fields, or a bare answer
    string; reject anything else with a specific reason."""
    if isinstance(raw, str):
        return AdapterOutput(answer=raw)
    if isinstance(raw, AdapterOutput):
        data = dataclasses.asdict(raw)
    elif isinstance(raw, dict):
        data = raw
    else:
        raise AdapterOutputError(f"adapter returned {type(raw).__name__}; expected AdapterOutput, dict, or str")

    answer = data.get("answer")
    if not isinstance(answer, str):
        raise AdapterOutputError("adapter output 'answer' must be a string")
    model = data.get("model")
    if model is not None and not isinstance(model, str):
        raise AdapterOutputError("adapter output 'model' must be a string or null")
    return AdapterOutput(
        answer=answer,
        retrieved_doc_ids=_string_list(data, "retrieved_doc_ids"),
        citations=_string_list(data, "citations"),
        input_tokens=_token_count(data, "input_tokens"),
        output_tokens=_token_count(data, "output_tokens"),
        model=model,
        steps=_steps(data),
    )


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AdapterOutputError(f"adapter output '{key}' must be a list of strings")
    return value


_STEP_KINDS = ("retrieval", "tool_call", "final_answer")
_STEP_KEYS = {"kind", "name", "args", "result", "error", "retrieved_doc_ids", "output", "duration_ms", "span_id"}
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def _json_value(where: str, value: Any) -> Any:
    """Steps are stored as JSON: reject what JSON can't hold (rather than
    letting the DB write fail, or `default=str` quietly change the value)."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AdapterOutputError(f"{where} is not JSON-serializable: {exc}") from exc
    return value


def _steps(data: dict[str, Any]) -> list[Step]:
    raw = data.get("steps") or []
    if not isinstance(raw, list):
        raise AdapterOutputError("adapter output 'steps' must be a list")
    steps = []
    for i, item in enumerate(raw):
        where = f"adapter output steps[{i}]"
        if not isinstance(item, dict):
            raise AdapterOutputError(f"{where} must be an object")
        unknown = sorted(set(item) - _STEP_KEYS)
        if unknown:
            raise AdapterOutputError(f"{where} has unknown key(s) {unknown}")
        kind = item.get("kind")
        if kind not in _STEP_KINDS:
            raise AdapterOutputError(f"{where}.kind must be one of {list(_STEP_KINDS)}")
        name = item.get("name") or ""
        if not isinstance(name, str):
            raise AdapterOutputError(f"{where}.name must be a string")
        if kind == "tool_call" and not name.strip():
            raise AdapterOutputError(f"{where}: a tool_call step needs the tool's name")
        args = item.get("args") or {}
        if not isinstance(args, dict):
            raise AdapterOutputError(f"{where}.args must be an object")
        error = item.get("error")
        if error is not None and not isinstance(error, str):
            raise AdapterOutputError(f"{where}.error must be a string or null")
        output = item.get("output")
        if output is not None and not isinstance(output, str):
            raise AdapterOutputError(f"{where}.output must be a string or null")
        duration = item.get("duration_ms")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration < 0
            or not math.isfinite(duration)
        ):
            raise AdapterOutputError(f"{where}.duration_ms must be a non-negative number or null")
        span_id = item.get("span_id")
        if span_id is not None and (not isinstance(span_id, str) or not _SPAN_ID.match(span_id)):
            raise AdapterOutputError(f"{where}.span_id must be 16 lowercase hex characters or null")
        steps.append(
            Step(
                kind=kind,
                name=name,
                args=_json_value(f"{where}.args", args),
                result=_json_value(f"{where}.result", item.get("result")),
                error=error,
                retrieved_doc_ids=_string_list(item, "retrieved_doc_ids"),
                output=output,
                duration_ms=float(duration) if duration is not None else None,
                span_id=span_id,
            )
        )
    return steps


def _token_count(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise AdapterOutputError(f"adapter output '{key}' must be a non-negative integer or null")
    return value


def _describe(exc: BaseException) -> tuple[str, str]:
    error_type = f"{type(exc).__module__}.{type(exc).__qualname__}".removeprefix("builtins.")
    tb = "".join(traceback.format_exception(exc)).strip()
    message = str(exc) or type(exc).__name__
    detail = f"{message}\n\n{tb}"
    if len(detail) > _MAX_ERROR_CHARS:
        detail = detail[:_MAX_ERROR_CHARS] + "\n... (truncated)"
    return error_type, detail


def _call_in_daemon_thread(fn: Callable[[], Any]) -> concurrent.futures.Future:
    future: concurrent.futures.Future = concurrent.futures.Future()
    # A new thread starts with an empty context; carry the caller's (the
    # current span, among others) into it.
    ctx = contextvars.copy_context()

    def target() -> None:
        start = time.perf_counter()
        error: BaseException | None = None
        result: Any = None
        try:
            result = ctx.run(fn)
        except BaseException as exc:  # noqa: BLE001 - includes SystemExit; reported, never re-raised here
            error = exc
        elapsed_ms = (time.perf_counter() - start) * 1000
        # If the case already timed out, the future was cancelled and this
        # late outcome is simply discarded.
        try:
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result((result, elapsed_ms))
        except concurrent.futures.InvalidStateError:
            pass

    threading.Thread(target=target, name="agentforge-adapter-case", daemon=True).start()
    return future


class PythonAdapter:
    def __init__(self, target: str) -> None:
        module_path, _, func_name = target.partition(":")
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 - import-time errors in trusted code
            raise AdapterLoadError(f"could not import adapter module '{module_path}': {exc}") from exc
        fn = getattr(module, func_name, None)
        if fn is None or not callable(fn):
            raise AdapterLoadError(f"module '{module_path}' has no callable '{func_name}'")
        self._fn = fn
        self._target = target
        self.replay_settings: ReplaySettings | None = replay_settings(fn)
        self._is_async = inspect.iscoroutinefunction(fn)
        try:
            self._takes_scenario = "scenario" in inspect.signature(fn).parameters
        except (TypeError, ValueError):  # no introspectable signature (some builtins/C callables)
            self._takes_scenario = False

    async def call(
        self,
        input_text: str,
        case_key: str,
        timeout: float,
        scenario: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[Any, float | None]:
        kwargs: dict[str, Any] = {}
        if overrides:
            kwargs["overrides"] = overrides  # validated by check_overrides against what the adapter declared
        if scenario is not None:
            if not self._takes_scenario:
                raise ScenarioNotSupportedError(
                    f"this case has a scenario (keys: {sorted(scenario)}), but adapter '{self._target}' takes no "
                    "`scenario` argument; running it without its setup would test something else"
                )
            kwargs["scenario"] = scenario
        if self._is_async:
            return await asyncio.wait_for(self._fn(input_text, **kwargs), timeout), None
        future = _call_in_daemon_thread(lambda: self._fn(input_text, **kwargs))
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)

    async def aclose(self) -> None:
        pass


class HttpAdapter:
    # An http adapter has no way to declare replay settings (yet).
    replay_settings: ReplaySettings | None = None

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = httpx.AsyncClient()

    async def call(
        self,
        input_text: str,
        case_key: str,
        timeout: float,
        scenario: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[Any, float | None]:
        if overrides:
            raise AdapterLoadError("http adapters can't be given replay overrides")
        body: dict[str, Any] = {"input": input_text, "case_key": case_key}
        if scenario is not None:
            body["scenario"] = scenario
        headers: dict[str, str] = {}
        propagate.inject(headers)  # traceparent for the current (invoke_agent) span; nothing if not tracing
        response = await asyncio.wait_for(
            self._client.post(self._url, json=body, headers=headers, timeout=timeout), timeout
        )
        if response.status_code // 100 != 2:
            raise AdapterOutputError(f"HTTP adapter returned {response.status_code}: {response.text[:500]}")
        try:
            return response.json(), None
        except ValueError as exc:
            raise AdapterOutputError(f"HTTP adapter returned non-JSON body: {response.text[:200]!r}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def check_overrides(adapter: PythonAdapter | HttpAdapter, overrides: dict[str, Any] | None, target: str) -> dict:
    """The replay's overrides, validated against what the adapter declared
    (agentforge_sdk.replay.OverrideError if they don't match)."""
    return validate_overrides(adapter.replay_settings, overrides, target=target)


def build_adapter(adapter_type: str | None, target: str | None) -> PythonAdapter | HttpAdapter:
    if adapter_type == "python" and target:
        return PythonAdapter(target)
    if adapter_type == "http" and target:
        return HttpAdapter(target)
    raise AdapterLoadError(f"unsupported adapter: type={adapter_type!r} target={target!r}")


async def invoke(
    adapter: PythonAdapter | HttpAdapter,
    input_text: str,
    case_key: str,
    timeout: float,
    scenario: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> CaseOutcome:
    start = time.perf_counter()

    def elapsed() -> float:
        return (time.perf_counter() - start) * 1000

    try:
        raw, measured_ms = await adapter.call(input_text, case_key, timeout, scenario, overrides)
        output = coerce_output(raw)
    except asyncio.CancelledError:
        raise  # the worker itself is being cancelled -- not the adapter's fault
    except (TimeoutError, httpx.TimeoutException):
        return CaseOutcome(
            status="timeout",
            latency_ms=elapsed(),
            error_type="timeout",
            error_message=f"adapter did not return within the per-case timeout of {timeout:g}s",
        )
    except BaseException as exc:  # noqa: BLE001 - includes SystemExit/KeyboardInterrupt raised by the adapter
        error_type, message = _describe(exc)
        return CaseOutcome(status="error", latency_ms=elapsed(), error_type=error_type, error_message=message)
    return CaseOutcome(status="ok", latency_ms=measured_ms if measured_ms is not None else elapsed(), output=output)
