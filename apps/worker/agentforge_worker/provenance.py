"""What code and pricing a run or replay executed with.

Recorded on every run and replay, so a replay can say whether it ran the
same thing as the run it replays:

* `code_version` -- for people: the git commit the worker image was built
  from (AGENTFORGE_GIT_SHA, a build arg set by the scripts and CI; a
  `-dirty` suffix means uncommitted changes), else the worker package
  version.
* `code_sha256` -- what's compared: a hash of the Python source the case
  ran through -- AgentForge's own packages (schemas, evaluators, SDK, API
  models, worker) and the adapter's top-level package. Line endings are
  normalized. It catches uncommitted edits a git SHA wouldn't, and costs a
  few ms once per process and adapter.
* `pricing_sha256` -- the pricing file (AGENTFORGE_PRICING_FILE) the worker
  loaded, or None when there is none.

These describe the worker process, not the agent's behavior: a model
behind an HTTP adapter, or an external service, can change without any of
them changing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from functools import cache
from importlib import metadata
from pathlib import Path

_AGENTFORGE_PACKAGES = (
    "agentforge_core",
    "agentforge_evaluators",
    "agentforge_sdk",
    "agentforge_api",
    "agentforge_worker",
)


def code_version() -> str:
    sha = os.environ.get("AGENTFORGE_GIT_SHA", "").strip()
    if sha:
        return sha
    try:
        return f"agentforge-worker {metadata.version('agentforge-worker')} (no git commit recorded)"
    except metadata.PackageNotFoundError:
        return "unknown"


def _package_files(name: str) -> list[Path]:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return []
    if spec is None:
        return []
    if spec.submodule_search_locations:
        files: list[Path] = []
        for location in spec.submodule_search_locations:
            files.extend(p for p in Path(location).rglob("*.py") if "__pycache__" not in p.parts)
        return files
    return [Path(spec.origin)] if spec.origin and spec.origin.endswith(".py") else []


@cache
def code_sha256(adapter_type: str | None, adapter_target: str | None) -> str:
    packages = list(_AGENTFORGE_PACKAGES)
    if adapter_type == "python" and adapter_target:
        top = adapter_target.partition(":")[0].split(".")[0]
        if top not in packages:
            packages.append(top)
    digest = hashlib.sha256()
    for name in packages:
        relative = {f: f.relative_to(_root(f, name)).as_posix() for f in _package_files(name)}
        for f in sorted(relative, key=relative.__getitem__):
            digest.update(f"{name}/{relative[f]}\0".encode())
            digest.update(f.read_bytes().replace(b"\r\n", b"\n"))
            digest.update(b"\0")
    return digest.hexdigest()


def _root(file: Path, package: str) -> Path:
    """The package's directory (the ancestor named after it), or the file's own directory."""
    for parent in file.parents:
        if parent.name == package:
            return parent
    return file.parent


@cache
def pricing_sha256() -> str | None:
    """Hash of the pricing file, read once per process (when the worker loads it)."""
    path = os.environ.get("AGENTFORGE_PRICING_FILE")
    if not path or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def collect(adapter_type: str | None, adapter_target: str | None) -> dict[str, str | None]:
    return {
        "code_version": code_version(),
        "code_sha256": code_sha256(adapter_type, adapter_target),
        "pricing_sha256": pricing_sha256(),
    }
