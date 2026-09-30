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
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import importlib
import inspect
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from agentforge_sdk import AdapterOutput

_MAX_ERROR_CHARS = 4000


class AdapterLoadError(Exception):
    """The adapter itself can't be set up (bad import path, not callable)."""


class AdapterOutputError(Exception):
    """The adapter returned something that isn't a valid AdapterOutput."""


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
    lists = {}
    for key in ("retrieved_doc_ids", "citations"):
        value = data.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise AdapterOutputError(f"adapter output '{key}' must be a list of strings")
        lists[key] = value
    tokens = {}
    for key in ("input_tokens", "output_tokens"):
        value = data.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise AdapterOutputError(f"adapter output '{key}' must be a non-negative integer or null")
        tokens[key] = value
    model = data.get("model")
    if model is not None and not isinstance(model, str):
        raise AdapterOutputError("adapter output 'model' must be a string or null")
    return AdapterOutput(answer=answer, model=model, **lists, **tokens)


def _describe(exc: BaseException) -> tuple[str, str]:
    error_type = f"{type(exc).__module__}.{type(exc).__qualname__}".removeprefix("builtins.")
    tb = "".join(traceback.format_exception(exc)).strip()
    message = str(exc) or type(exc).__name__
    detail = f"{message}\n\n{tb}"
    if len(detail) > _MAX_ERROR_CHARS:
        detail = detail[:_MAX_ERROR_CHARS] + "\n... (truncated)"
    return error_type, detail


def _call_in_daemon_thread(fn: Callable[[str], Any], input_text: str) -> concurrent.futures.Future:
    future: concurrent.futures.Future = concurrent.futures.Future()

    def target() -> None:
        start = time.perf_counter()
        try:
            result = fn(input_text)
        except BaseException as exc:  # noqa: BLE001 - includes SystemExit; reported, never re-raised here
            outcome = ("exception", exc)
        else:
            outcome = ("result", (result, (time.perf_counter() - start) * 1000))
        # If the case already timed out, the future was cancelled and this
        # late outcome is simply discarded.
        try:
            if outcome[0] == "exception":
                future.set_exception(outcome[1])
            else:
                future.set_result(outcome[1])
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
        self._is_async = inspect.iscoroutinefunction(fn)

    async def call(self, input_text: str, case_key: str, timeout: float) -> tuple[Any, float | None]:
        if self._is_async:
            return await asyncio.wait_for(self._fn(input_text), timeout), None
        return await asyncio.wait_for(asyncio.wrap_future(_call_in_daemon_thread(self._fn, input_text)), timeout)

    async def aclose(self) -> None:
        pass


class HttpAdapter:
    def __init__(self, url: str) -> None:
        self._url = url
        self._client = httpx.AsyncClient()

    async def call(self, input_text: str, case_key: str, timeout: float) -> tuple[Any, float | None]:
        response = await asyncio.wait_for(
            self._client.post(self._url, json={"input": input_text, "case_key": case_key}, timeout=timeout),
            timeout,
        )
        if response.status_code // 100 != 2:
            raise AdapterOutputError(f"HTTP adapter returned {response.status_code}: {response.text[:500]}")
        try:
            return response.json(), None
        except ValueError as exc:
            raise AdapterOutputError(f"HTTP adapter returned non-JSON body: {response.text[:200]!r}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def build_adapter(adapter_type: str | None, target: str | None) -> PythonAdapter | HttpAdapter:
    if adapter_type == "python" and target:
        return PythonAdapter(target)
    if adapter_type == "http" and target:
        return HttpAdapter(target)
    raise AdapterLoadError(f"unsupported adapter: type={adapter_type!r} target={target!r}")


async def invoke(adapter: PythonAdapter | HttpAdapter, input_text: str, case_key: str, timeout: float) -> CaseOutcome:
    start = time.perf_counter()

    def elapsed() -> float:
        return (time.perf_counter() - start) * 1000

    try:
        raw, measured_ms = await adapter.call(input_text, case_key, timeout)
        output = coerce_output(raw)
    except asyncio.CancelledError:
        raise  # the worker itself is being cancelled -- not the adapter's fault
    except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
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
