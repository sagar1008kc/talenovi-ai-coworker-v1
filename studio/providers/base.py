"""Provider-neutral job machinery for paid asynchronous media generation.

Guarantees:
- A request is submitted at most once per job key. Its request ID is persisted before polling,
  so an interrupted run resumes polling instead of paying for a second generation.
- Every submission reserves its worst-case estimated cost in the CostLedger first.
- Transient HTTP failures (connection errors, 408/409/429/5xx) back off exponentially with jitter.
- A wall-clock timeout cancels the job (best effort) and the cost is kept as "possibly billed".
- Completed media is streamed to a temporary file, size-capped, magic-byte checked and moved
  into place atomically.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import json
import logging
import math
from pathlib import Path
import random
import threading
import time
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, Field

from studio.budget import CostLedger
from studio.models import CameraMove, utc_now

log = logging.getLogger("studio.providers")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- errors
class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class JobFailed(ProviderError):
    """The provider accepted the job but reported a generation failure."""


class JobTimeout(ProviderError):
    """The job did not finish before the configured deadline."""


# --------------------------------------------------------------------------- HTTP seam
class HttpResponse(Protocol):
    status_code: int
    headers: Any
    text: str

    def json(self) -> Any: ...
    def iter_content(self, chunk_size: int = ...) -> Any: ...


class HttpClient(Protocol):
    """Subset of requests.Session used here; tests inject a fake."""

    def get(self, url: str, **kw) -> HttpResponse: ...
    def post(self, url: str, **kw) -> HttpResponse: ...
    def put(self, url: str, **kw) -> HttpResponse: ...
    def delete(self, url: str, **kw) -> HttpResponse: ...


def default_http() -> HttpClient:
    import requests
    return requests.Session()


def check_response(resp: HttpResponse, what: str) -> Any:
    """Raise a classified ProviderError for non-2xx; return parsed JSON otherwise."""
    if 200 <= resp.status_code < 300:
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{what}: invalid JSON response", retryable=True) from exc
    body = (getattr(resp, "text", "") or "")[:400]
    raise ProviderError(f"{what}: HTTP {resp.status_code}: {body}",
                        retryable=resp.status_code in RETRYABLE_STATUS, status=resp.status_code)


# --------------------------------------------------------------------------- retry policy
class RetryPolicy(BaseModel):
    submit_attempts: int = 4
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    poll_initial_s: float = 5.0
    poll_factor: float = 1.6
    poll_max_s: float = 30.0
    poll_error_budget: int = 8
    timeout_s: float = 20 * 60
    download_attempts: int = 3
    max_download_bytes: int = 400_000_000


def backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    """Exponential backoff with full jitter: attempt 0 -> ~base."""
    ceiling = min(policy.max_delay_s, policy.base_delay_s * (2 ** attempt))
    return random.uniform(ceiling / 2, ceiling)


def with_retries(fn: Callable[[], Any], policy: RetryPolicy, sleep: Callable[[float], None],
                 what: str, attempts: int | None = None) -> Any:
    attempts = attempts or policy.submit_attempts
    for attempt in range(attempts):
        try:
            return fn()
        except ProviderError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
            delay = backoff_delay(attempt, policy)
            log.warning("retrying", extra={"event": "retry", "what": what, "attempt": attempt + 1,
                                           "delay_s": round(delay, 2), "error": str(exc)})
            sleep(delay)
        except OSError as exc:  # requests.ConnectionError / Timeout subclass OSError
            if attempt == attempts - 1:
                raise ProviderError(f"{what}: network error: {exc}", retryable=True) from exc
            sleep(backoff_delay(attempt, policy))
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- persisted jobs
JobStatus = Literal["submitted", "completed", "downloaded", "failed", "timeout"]


class JobRecord(BaseModel):
    key: str
    provider: str
    model_id: str
    request_id: str
    status: JobStatus = "submitted"
    status_url: str | None = None
    response_url: str | None = None
    cancel_url: str | None = None
    submitted_utc: str = Field(default_factory=utc_now)
    submitted_epoch: float = Field(default_factory=time.time)
    cost_entry_id: str | None = None
    estimated_usd: float = 0.0
    payload_summary: dict = Field(default_factory=dict)
    media_url: str | None = None
    output_path: str | None = None
    error: str | None = None


class JobStore:
    """JSON file of every paid request for one video (assets/jobs.json)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._jobs = {k: JobRecord.model_validate(v) for k, v in raw.items()}

    def get(self, key: str) -> JobRecord | None:
        return self._jobs.get(key)

    def put(self, record: JobRecord) -> None:
        with self._lock:
            self._jobs[record.key] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({k: v.model_dump() for k, v in self._jobs.items()}, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)

    def all(self) -> list[JobRecord]:
        return list(self._jobs.values())

    def retire(self, prefix: str, statuses: tuple[str, ...] | None = None,
               max_retirements: int | None = None, exact: bool = False) -> int:
        """Move jobs under `prefix` aside as '<key>#retiredN' history so the next run submits a fresh job.

        statuses limits which records move (None = any). Returns how many moved; 0 if the retirement
        budget for this prefix is used up."""
        with self._lock:
            def live(k: str) -> bool:
                return "#retired" not in k and (k == prefix if exact else k.startswith(prefix))
            used = sum(1 for k in self._jobs if "#retired" in k and live(k.split("#retired")[0]))
            if max_retirements is not None and used >= max_retirements:
                return 0
            keys = [k for k, v in self._jobs.items() if live(k) and (statuses is None or v.status in statuses)]
            for n, k in enumerate(keys, start=used + 1):
                record = self._jobs.pop(k)
                self._jobs[f"{k}#retired{n}"] = record.model_copy(update={"key": f"{k}#retired{n}"})
            if keys:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps({k: v.model_dump() for k, v in self._jobs.items()}, indent=2),
                               encoding="utf-8")
                tmp.replace(self.path)
            return len(keys)

    def retire_failed(self, prefix: str, max_retirements: int) -> bool:
        """Allow one more attempt after a failed/timed-out job, up to max_retirements per prefix."""
        return self.retire(prefix, ("failed", "timeout"), max_retirements) > 0


# --------------------------------------------------------------------------- adapters
class PollResult(BaseModel):
    state: Literal["pending", "done", "failed"]
    error: str | None = None
    media_url: str | None = None  # some providers return the URL in the status payload


class JobAdapter(ABC):
    """Transport for one platform (fal queue, Runway tasks)."""
    platform: str

    @abstractmethod
    def submit(self, model_id: str, payload: dict) -> dict:
        """Return dict with request_id and optional status_url/response_url/cancel_url."""

    @abstractmethod
    def poll(self, record: JobRecord) -> PollResult: ...

    @abstractmethod
    def media_url(self, record: JobRecord, kind: Literal["video", "image"]) -> str: ...

    @abstractmethod
    def cancel(self, record: JobRecord) -> None: ...


MAGIC = {
    "video": lambda head: len(head) >= 12 and head[4:8] == b"ftyp",
    "image": lambda head: head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8")
    or head[8:12] == b"WEBP",
}


def download_file(http: HttpClient, url: str, dest: Path, kind: Literal["video", "image"],
                  policy: RetryPolicy, sleep: Callable[[float], None]) -> Path:
    """Stream a provider result URL to disk atomically with size and format checks."""
    if not url.startswith("https://"):
        raise ProviderError(f"Refusing non-HTTPS media URL: {url[:80]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    def attempt() -> Path:
        resp = http.get(url, stream=True, timeout=(15, 300))
        if not 200 <= resp.status_code < 300:
            raise ProviderError(f"download HTTP {resp.status_code}",
                                retryable=resp.status_code in RETRYABLE_STATUS,
                                status=resp.status_code)
        size = 0
        head = b""
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                if len(head) < 16:
                    head += chunk[:16]
                size += len(chunk)
                if size > policy.max_download_bytes:
                    raise ProviderError("download exceeds size cap")
                fh.write(chunk)
        if size == 0 or not MAGIC[kind](head):
            raise ProviderError(f"downloaded {kind} failed format check", retryable=True)
        tmp.replace(dest)
        return dest

    try:
        return with_retries(attempt, policy, sleep, f"download {dest.name}",
                            attempts=policy.download_attempts)
    finally:
        tmp.unlink(missing_ok=True)


def run_job(*, adapter: JobAdapter, http: HttpClient, store: JobStore, ledger: CostLedger,
            key: str, model_id: str, payload: dict, estimated_usd: float, item: str,
            dest: Path, kind: Literal["video", "image"], policy: RetryPolicy,
            sleep: Callable[[float], None] = time.sleep,
            now: Callable[[], float] = time.time,
            should_stop: Callable[[], bool] = lambda: False) -> JobRecord:
    """Submit (once), poll, download. Safe to call again after any interruption."""
    record = store.get(key)
    if record and record.status == "downloaded" and record.output_path and Path(record.output_path).is_file():
        return record
    if record and record.status in ("failed", "timeout"):
        raise JobFailed(f"{key}: earlier attempt {record.status}: {record.error}")

    if record is None:
        entry = ledger.reserve(adapter.platform, model_id, item, estimated_usd, job_key=key)
        try:
            ids = with_retries(lambda: adapter.submit(model_id, payload), policy, sleep,
                               f"submit {key}")
        except ProviderError as exc:
            ledger.settle(entry, "failed_unbilled", error=str(exc))
            raise
        record = JobRecord(key=key, provider=adapter.platform, model_id=model_id,
                           request_id=ids["request_id"], status_url=ids.get("status_url"),
                           response_url=ids.get("response_url"), cancel_url=ids.get("cancel_url"),
                           cost_entry_id=entry, estimated_usd=estimated_usd, submitted_epoch=now(),
                           payload_summary={k: v for k, v in payload.items()
                                            if not str(v).startswith("data:")})
        store.put(record)  # persist BEFORE polling
        log.info("job submitted", extra={"event": "job_submitted", "key": key,
                                         "provider": adapter.platform, "model": model_id,
                                         "request_id": record.request_id,
                                         "estimated_usd": estimated_usd})

    deadline = record.submitted_epoch + policy.timeout_s
    delay = policy.poll_initial_s
    poll_errors = 0
    first_poll = True
    while record.status == "submitted":
        if should_stop():
            raise KeyboardInterrupt("stop requested; job remains resumable")
        if not first_poll and now() > deadline:
            try:
                adapter.cancel(record)
            except ProviderError as exc:
                log.warning("cancel failed", extra={"event": "cancel_failed", "error": str(exc)})
            record.status, record.error = "timeout", f"exceeded {policy.timeout_s:.0f}s"
            store.put(record)
            ledger.settle(record.cost_entry_id, "possibly_billed", error=record.error)
            raise JobTimeout(f"{key}: {record.error}")
        try:
            result = adapter.poll(record)
            poll_errors = 0
        except (ProviderError, OSError) as exc:
            poll_errors += 1
            if poll_errors > policy.poll_error_budget or (isinstance(exc, ProviderError) and not exc.retryable):
                raise
            sleep(min(policy.poll_max_s, delay))
            continue
        first_poll = False
        if result.state == "done":
            record.status, record.media_url = "completed", result.media_url
            store.put(record)
            break
        if result.state == "failed":
            record.status, record.error = "failed", result.error or "provider reported failure"
            store.put(record)
            ledger.settle(record.cost_entry_id, "failed_unbilled", error=record.error)
            raise JobFailed(f"{key}: {record.error}")
        if now() <= deadline:
            sleep(delay)
            delay = min(policy.poll_max_s, delay * policy.poll_factor)

    if record.status == "completed":
        url = record.media_url or with_retries(lambda: adapter.media_url(record, kind), policy, sleep,
                                               f"result {key}")
        record.media_url = url
        download_file(http, url, dest, kind, policy, sleep)
        record.status, record.output_path = "downloaded", str(dest)
        store.put(record)
        ledger.settle(record.cost_entry_id, "ok", actual_usd=record.estimated_usd)
        log.info("job downloaded", extra={"event": "job_downloaded", "key": key, "path": str(dest)})
    return record


# --------------------------------------------------------------------------- video interface
CAMERA_PHRASES: dict[str, str] = {
    "zoom_in": "Camera: slow, steady dolly-in toward the subject.",
    "zoom_out": "Camera: slow pull-back revealing the wider scene.",
    "pan_left": "Camera: smooth lateral pan from right to left.",
    "pan_right": "Camera: smooth lateral pan from left to right.",
    "push_slow": "Camera: gentle handheld-free push forward, cinematic and stable.",
}


class VideoRequest(BaseModel):
    shot_id: str
    mode: Literal["image_to_video", "text_to_video"]
    prompt: str
    image_path: Path | None = None
    seconds_needed: float
    aspect_ratio: Literal["16:9", "9:16"]
    resolution: str = "720p"
    camera: CameraMove = "push_slow"


class ModelSpec(BaseModel):
    key: str
    platform: Literal["fal", "runway"]
    label: str
    mode: Literal["image_to_video", "text_to_video"]
    model_id: str
    image_field: str | None = None
    duration: dict
    resolution: dict
    aspect_ratio: dict | None = None
    fixed_args: dict = Field(default_factory=dict)
    usd_per_second: dict[str, float]
    source: str
    notes: str = ""


class VideoGenerationProvider:
    """One catalog model on one transport. Builds verified payloads and estimates cost."""

    def __init__(self, spec: ModelSpec, adapter: JobAdapter, image_encoder: Callable[[Path], str] | None):
        self.spec, self.adapter, self.image_encoder = spec, adapter, image_encoder

    @property
    def name(self) -> str:
        return self.spec.key

    def supports(self, req: VideoRequest) -> bool:
        if req.mode != self.spec.mode:
            return False
        if self.spec.mode == "image_to_video" and (req.image_path is None or self.image_encoder is None):
            return False
        aspect = self.spec.aspect_ratio
        return aspect is None or req.aspect_ratio in aspect.get("values", [])

    def choose_duration(self, seconds_needed: float) -> int:
        values = sorted(int(v) for v in self.spec.duration["values"])
        need = math.ceil(max(seconds_needed, 0.1))
        return next((v for v in values if v >= need), values[-1])

    def choose_resolution(self, wanted: str) -> str:
        values = self.spec.resolution["values"]
        if wanted in values:
            return wanted
        priced = [v for v in values if v in self.spec.usd_per_second]
        return priced[0] if priced else values[0]

    def estimate_usd(self, req: VideoRequest) -> float:
        res = self.choose_resolution(req.resolution)
        return self.choose_duration(req.seconds_needed) * self.spec.usd_per_second[res]

    def build_payload(self, req: VideoRequest) -> dict:
        seconds = self.choose_duration(req.seconds_needed)
        fmt = self.spec.duration.get("format", "int")
        duration_value: Any = {"int": seconds, "str": str(seconds), "seconds_suffix": f"{seconds}s"}[fmt]
        prompt = f"{req.prompt.strip()} {CAMERA_PHRASES[req.camera]} No text, no captions, no logos."
        payload: dict[str, Any] = dict(self.spec.fixed_args)
        payload["prompt"] = prompt
        payload[self.spec.duration["param"]] = duration_value
        if self.spec.resolution.get("param"):
            payload[self.spec.resolution["param"]] = self.choose_resolution(req.resolution)
        if self.spec.aspect_ratio and self.spec.aspect_ratio.get("param"):
            mapping = self.spec.aspect_ratio.get("map", {})
            payload[self.spec.aspect_ratio["param"]] = mapping.get(req.aspect_ratio, req.aspect_ratio)
        if self.spec.mode == "image_to_video":
            assert req.image_path is not None and self.image_encoder is not None
            payload[self.spec.image_field or "image_url"] = self.image_encoder(req.image_path)
        return payload


def generate_clip(providers: list[VideoGenerationProvider], req: VideoRequest, *, http: HttpClient,
                  store: JobStore, ledger: CostLedger, dest: Path, policy: RetryPolicy,
                  sleep: Callable[[float], None] = time.sleep,
                  should_stop: Callable[[], bool] = lambda: False) -> tuple[VideoGenerationProvider, JobRecord]:
    """Try providers in fallback order. Budget errors are never swallowed."""
    errors: list[str] = []
    for provider in providers:
        if not provider.supports(req):
            continue
        key = f"clip:{req.shot_id}:{provider.name}"
        existing = store.get(key)
        if existing and existing.status in ("failed", "timeout"):
            errors.append(f"{provider.name}: {existing.status}")
            continue
        try:
            payload = provider.build_payload(req) if existing is None else {}
            record = run_job(adapter=provider.adapter, http=http, store=store, ledger=ledger, key=key,
                             model_id=provider.spec.model_id, payload=payload,
                             estimated_usd=provider.estimate_usd(req), item=f"motion clip {req.shot_id}",
                             dest=dest, kind="video", policy=policy, sleep=sleep, should_stop=should_stop)
            return provider, record
        except ProviderError as exc:
            errors.append(f"{provider.name}: {exc}")
            log.warning("provider failed; trying fallback",
                        extra={"event": "provider_fallback", "shot": req.shot_id,
                               "provider": provider.name, "error": str(exc)})
    raise ProviderError(f"All video providers failed for {req.shot_id}: {errors}")
