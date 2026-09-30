"""A deliberately faulty adapter, for demonstrating and testing that one
bad case never takes down a run or the worker.

Behavior is selected by a marker in the test case's input text:

* ``[[sleep:<seconds>]]`` -- blocks for that long (use it with a smaller
  per-case timeout to produce a timeout result)
* ``[[crash]]``           -- raises RuntimeError
* ``[[exit]]``            -- raises SystemExit (a BaseException; must not kill the worker)
* ``[[bad-output]]``      -- returns something that isn't an AdapterOutput
* anything else           -- delegates to the real example adapter

This is a test fixture, not an example of a real application.
"""

from __future__ import annotations

import re
import time

from rag_app.adapter import answer as real_answer

_SLEEP = re.compile(r"\[\[sleep:(\d+(?:\.\d+)?)\]\]")


def answer(input_text: str):
    sleep = _SLEEP.search(input_text)
    if sleep:
        time.sleep(float(sleep.group(1)))
    if "[[crash]]" in input_text:
        raise RuntimeError("fault injection: adapter crashed on purpose")
    if "[[exit]]" in input_text:
        raise SystemExit("fault injection: adapter called sys.exit")
    if "[[bad-output]]" in input_text:
        return 42
    return real_answer(input_text)
