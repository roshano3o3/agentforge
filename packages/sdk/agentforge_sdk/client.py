"""Thin synchronous HTTP client for the AgentForge API.

Kept deliberately small: it knows the API's URL shape and nothing else. No
SQLAlchemy, no ORM types, no server-side imports — safe to embed in any
application's codebase.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """A non-2xx API response, with the server's `detail` message."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise ApiError(response.status_code, str(detail))


class AgentForgeClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AgentForgeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- applications --------------------------------------------------

    def upsert_application(self, name: str, description: str | None = None) -> dict[str, Any]:
        resp = self._client.post("/applications", json={"name": name, "description": description})
        _raise_for_status(resp)
        return resp.json()

    def upsert_application_version(
        self, application_id: str, version: str, description: str | None = None
    ) -> dict[str, Any]:
        resp = self._client.post(
            f"/applications/{application_id}/versions",
            json={"version": version, "description": description},
        )
        _raise_for_status(resp)
        return resp.json()

    # -- datasets ---------------------------------------------------------

    def upsert_dataset(self, name: str, description: str | None = None) -> dict[str, Any]:
        resp = self._client.post("/datasets", json={"name": name, "description": description})
        _raise_for_status(resp)
        return resp.json()

    def create_draft_version(
        self,
        dataset_id: str,
        test_cases: list[dict[str, Any]],
        default_evaluators: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Creates a new DRAFT dataset version (optionally pre-populated, with
        an optional version-level evaluator config). Does not publish it --
        call `publish_dataset_version` for that.
        """
        resp = self._client.post(
            f"/datasets/{dataset_id}/versions",
            json={"test_cases": test_cases, "default_evaluators": default_evaluators},
        )
        _raise_for_status(resp)
        return resp.json()

    def publish_dataset_version(self, dataset_name: str, version: int) -> dict[str, Any]:
        resp = self._client.post(f"/datasets/{dataset_name}/versions/{version}/publish")
        _raise_for_status(resp)
        return resp.json()

    def get_dataset_version(self, dataset_name: str, version: int | str = "latest") -> dict[str, Any]:
        resp = self._client.get(f"/datasets/{dataset_name}/versions/{version}")
        _raise_for_status(resp)
        return resp.json()

    # -- runs ---------------------------------------------------------------

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Creates a *pending* run and queues it for the worker (HTTP 202).
        Results are produced by the worker -- poll `get_run` until the status
        is `completed` or `failed`."""
        resp = self._client.post("/runs", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def list_evaluators(self) -> list[dict[str, Any]]:
        resp = self._client.get("/evaluators")
        _raise_for_status(resp)
        return resp.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/runs/{run_id}")
        _raise_for_status(resp)
        return resp.json()

    def list_runs(self) -> list[dict[str, Any]]:
        resp = self._client.get("/runs")
        _raise_for_status(resp)
        return resp.json()
