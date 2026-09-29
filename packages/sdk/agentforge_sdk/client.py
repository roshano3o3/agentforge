"""Thin synchronous HTTP client for the AgentForge API.

Kept deliberately small: it knows the API's URL shape and nothing else. No
SQLAlchemy, no ORM types, no server-side imports — safe to embed in any
application's codebase.
"""

from __future__ import annotations

from typing import Any

import httpx


class AgentForgeClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AgentForgeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- applications --------------------------------------------------

    def upsert_application(self, name: str, description: str | None = None) -> dict[str, Any]:
        resp = self._client.post("/applications", json={"name": name, "description": description})
        resp.raise_for_status()
        return resp.json()

    def upsert_application_version(
        self, application_id: str, version: str, description: str | None = None
    ) -> dict[str, Any]:
        resp = self._client.post(
            f"/applications/{application_id}/versions",
            json={"version": version, "description": description},
        )
        resp.raise_for_status()
        return resp.json()

    # -- datasets ---------------------------------------------------------

    def upsert_dataset(self, name: str, description: str | None = None) -> dict[str, Any]:
        resp = self._client.post("/datasets", json={"name": name, "description": description})
        resp.raise_for_status()
        return resp.json()

    def publish_dataset_version(self, dataset_id: str, test_cases: list[dict[str, Any]]) -> dict[str, Any]:
        resp = self._client.post(f"/datasets/{dataset_id}/versions", json={"test_cases": test_cases})
        resp.raise_for_status()
        return resp.json()

    def get_dataset_version(self, dataset_name: str, version: int | str = "latest") -> dict[str, Any]:
        resp = self._client.get(f"/datasets/{dataset_name}/versions/{version}")
        resp.raise_for_status()
        return resp.json()

    # -- runs ---------------------------------------------------------------

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post("/runs", json=payload)
        resp.raise_for_status()
        return resp.json()

    def submit_results(self, run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        resp = self._client.post(f"/runs/{run_id}/results", json={"results": results})
        resp.raise_for_status()
        return resp.json()

    def complete_run(self, run_id: str, status: str = "completed") -> dict[str, Any]:
        resp = self._client.post(f"/runs/{run_id}/complete", json={"status": status})
        resp.raise_for_status()
        return resp.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/runs/{run_id}")
        resp.raise_for_status()
        return resp.json()

    def list_runs(self) -> list[dict[str, Any]]:
        resp = self._client.get("/runs")
        resp.raise_for_status()
        return resp.json()
