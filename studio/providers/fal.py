"""fal.ai queue transport (REST) for video models and FLUX still images.

Queue API (https://fal.ai/docs/model-apis/model-endpoints/queue):
  POST https://queue.fal.run/<model_id>          -> request_id, status_url, response_url, cancel_url
  GET  <status_url>                              -> status IN_QUEUE | IN_PROGRESS | COMPLETED (+ error)
  GET  <response_url>                            -> model output JSON
  PUT  <cancel_url>                              -> best-effort cancel
The returned URLs are persisted and reused verbatim (fal rejects some hand-built paths with 405).
"""
from __future__ import annotations

import base64
from io import BytesIO
import math
from pathlib import Path
import time
from typing import Callable, Literal

from PIL import Image

from studio.budget import CostLedger
from studio.providers.base import (HttpClient, JobAdapter, JobRecord, JobStore, PollResult,
                                   ProviderError, RetryPolicy, check_response, run_job)


class FalQueue(JobAdapter):
    platform = "fal"

    def __init__(self, http: HttpClient, api_key: str, base_url: str = "https://queue.fal.run"):
        if not api_key:
            raise ProviderError("FAL_KEY is not set")
        self.http, self.base_url = http, base_url.rstrip("/")
        self._headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}

    def submit(self, model_id: str, payload: dict) -> dict:
        resp = self.http.post(f"{self.base_url}/{model_id}", json=payload, headers=self._headers,
                              timeout=(15, 120))
        data = check_response(resp, f"fal submit {model_id}")
        if not data.get("request_id"):
            raise ProviderError("fal submit returned no request_id")
        return {k: data.get(k) for k in ("request_id", "status_url", "response_url", "cancel_url")}

    def _status_url(self, record: JobRecord) -> str:
        if not record.status_url:
            raise ProviderError("fal job has no persisted status_url")
        return record.status_url

    def poll(self, record: JobRecord) -> PollResult:
        resp = self.http.get(self._status_url(record), headers=self._headers, timeout=(15, 60))
        data = check_response(resp, "fal status")
        status = data.get("status")
        if status in ("IN_QUEUE", "IN_PROGRESS"):
            return PollResult(state="pending")
        if status == "COMPLETED":
            if data.get("error"):
                return PollResult(state="failed", error=f"{data.get('error_type', '')} {data['error']}".strip())
            return PollResult(state="done")
        raise ProviderError(f"fal returned unknown status {status!r}", retryable=True)

    def media_url(self, record: JobRecord, kind: Literal["video", "image"]) -> str:
        if not record.response_url:
            raise ProviderError("fal job has no persisted response_url")
        resp = self.http.get(record.response_url, headers=self._headers, timeout=(15, 60))
        data = check_response(resp, "fal result")
        try:
            return data["video"]["url"] if kind == "video" else data["images"][0]["url"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"fal result missing {kind} URL") from exc

    def cancel(self, record: JobRecord) -> None:
        if record.cancel_url:
            self.http.put(record.cancel_url, headers=self._headers, timeout=(15, 30))


def image_data_uri(path: Path, max_side: int = 1920, max_bytes: int = 3_300_000) -> str:
    """Encode a local still as a JPEG data URI for image-to-video inputs.

    Runway documents a 3.3 MB binary limit for data URIs; the same cap keeps fal payloads small.
    """
    with Image.open(path) as img:
        img = img.convert("RGB")
        scale = min(1.0, max_side / max(img.size))
        if scale < 1.0:
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        for quality in (92, 85, 75, 65):
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= max_bytes:
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    raise ProviderError(f"Cannot compress {path.name} under {max_bytes} bytes")


def fal_image_input(path: Path) -> str:
    """Prefer fal CDN upload when the optional fal_client package is installed."""
    try:
        import fal_client  # type: ignore
    except ImportError:
        return image_data_uri(path)
    return fal_client.upload_file(str(path))


class FalImageGenerator:
    """FLUX still images through the same persisted fal queue jobs."""

    def __init__(self, queue: FalQueue, http: HttpClient, spec: dict, policy: RetryPolicy):
        self.queue, self.http, self.spec, self.policy = queue, http, spec, policy

    def estimate_usd(self, size: dict[str, int]) -> float:
        megapixels = math.ceil(size["width"] * size["height"] / 1_000_000 * 100) / 100
        return megapixels * self.spec["usd_per_megapixel"]

    def generate(self, *, key: str, prompt: str, size: dict[str, int], dest: Path, store: JobStore,
                 ledger: CostLedger, sleep: Callable[[float], None] = time.sleep,
                 should_stop: Callable[[], bool] = lambda: False) -> JobRecord:
        lo, hi = self.spec["min_side_px"], self.spec["max_side_px"]
        if not all(lo <= size[d] <= hi and size[d] % 16 == 0 for d in ("width", "height")):
            raise ValueError(f"Image size {size} outside {lo}-{hi}px or not a multiple of 16")
        payload = dict(self.spec["fixed_args"], prompt=prompt, image_size=size)
        return run_job(adapter=self.queue, http=self.http, store=store, ledger=ledger, key=key,
                       model_id=self.spec["model_id"], payload=payload,
                       estimated_usd=self.estimate_usd(size), item=f"image {key}", dest=dest,
                       kind="image", policy=self.policy, sleep=sleep, should_stop=should_stop)
