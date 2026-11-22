"""Optional Runway API transport (https://docs.dev.runwayml.com).

POST /v1/image_to_video -> {id};  GET /v1/tasks/{id} -> status PENDING | THROTTLED | RUNNING |
SUCCEEDED (output[]) | FAILED (failure, failureCode) | CANCELLED;  DELETE /v1/tasks/{id} cancels.
Every request carries the required X-Runway-Version header.
"""
from __future__ import annotations

from typing import Literal

from studio.providers.base import (HttpClient, JobAdapter, JobRecord, PollResult, ProviderError,
                                   check_response)


class RunwayTasks(JobAdapter):
    platform = "runway"

    def __init__(self, http: HttpClient, api_secret: str, base_url: str, api_version: str):
        if not api_secret:
            raise ProviderError("RUNWAYML_API_SECRET is not set")
        self.http, self.base_url = http, base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_secret}", "X-Runway-Version": api_version,
                         "Content-Type": "application/json"}

    def submit(self, model_id: str, payload: dict) -> dict:
        body = {
            "model": model_id,
            "promptImage": payload["image_url"],
            "promptText": payload["prompt"][:1000],  # documented maxLength
            "ratio": payload["ratio"],
            "duration": int(payload["duration"]),
        }
        resp = self.http.post(f"{self.base_url}/v1/image_to_video", json=body, headers=self._headers,
                              timeout=(15, 120))
        data = check_response(resp, "runway submit")
        if not data.get("id"):
            raise ProviderError("runway submit returned no task id")
        return {"request_id": data["id"]}

    def _task(self, record: JobRecord) -> dict:
        resp = self.http.get(f"{self.base_url}/v1/tasks/{record.request_id}", headers=self._headers,
                             timeout=(15, 60))
        return check_response(resp, "runway task")

    def poll(self, record: JobRecord) -> PollResult:
        task = self._task(record)
        status = task.get("status")
        if status in ("PENDING", "THROTTLED", "RUNNING"):
            return PollResult(state="pending")
        if status == "SUCCEEDED":
            output = task.get("output") or []
            if not output:
                return PollResult(state="failed", error="SUCCEEDED without output")
            return PollResult(state="done", media_url=output[0])
        if status in ("FAILED", "CANCELLED"):
            return PollResult(state="failed",
                              error=f"{status}: {task.get('failureCode', '')} {task.get('failure', '')}".strip())
        raise ProviderError(f"runway returned unknown status {status!r}", retryable=True)

    def media_url(self, record: JobRecord, kind: Literal["video", "image"]) -> str:
        result = self.poll(record)
        if result.state != "done" or not result.media_url:
            raise ProviderError("runway task has no output URL")
        return result.media_url

    def cancel(self, record: JobRecord) -> None:
        self.http.delete(f"{self.base_url}/v1/tasks/{record.request_id}", headers=self._headers,
                         timeout=(15, 30))
