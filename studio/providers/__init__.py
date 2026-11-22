"""Pluggable media providers. Build them from the verified catalog in config/providers.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

from studio.providers.base import (HttpClient, JobStore, ModelSpec, ProviderError, RetryPolicy,
                                   VideoGenerationProvider, VideoRequest, default_http,
                                   generate_clip)
from studio.providers.fal import FalImageGenerator, FalQueue, fal_image_input, image_data_uri
from studio.providers.runway import RunwayTasks

__all__ = ["load_catalog", "build_video_providers", "build_image_generator", "VideoRequest",
           "VideoGenerationProvider", "JobStore", "RetryPolicy", "ProviderError", "generate_clip"]


def load_catalog(root: Path) -> dict:
    return json.loads((root / "config" / "providers.json").read_text(encoding="utf-8"))


def model_spec(catalog: dict, key: str) -> ModelSpec:
    try:
        raw = catalog["video_models"][key]
    except KeyError as exc:
        raise ValueError(f"Unknown video model {key!r}; choose from {sorted(catalog['video_models'])}") from exc
    return ModelSpec(key=key, **{k: v for k, v in raw.items()})


def build_video_providers(catalog: dict, order: list[str], http: HttpClient | None = None,
                          env: dict | None = None) -> list[VideoGenerationProvider]:
    """Return providers in fallback order, skipping platforms without credentials."""
    env = os.environ if env is None else env
    http = http or default_http()
    providers: list[VideoGenerationProvider] = []
    fal: FalQueue | None = None
    runway: RunwayTasks | None = None
    for key in order:
        spec = model_spec(catalog, key)
        if spec.platform == "fal":
            if not env.get("FAL_KEY"):
                continue
            fal = fal or FalQueue(http, env["FAL_KEY"], catalog["fal"]["queue_base_url"])
            providers.append(VideoGenerationProvider(spec, fal, fal_image_input))
        elif spec.platform == "runway":
            if not env.get("RUNWAYML_API_SECRET"):
                continue
            runway = runway or RunwayTasks(http, env["RUNWAYML_API_SECRET"],
                                           catalog["runway"]["base_url"], catalog["runway"]["api_version"])
            providers.append(VideoGenerationProvider(spec, runway, image_data_uri))
    return providers


def build_image_generator(catalog: dict, key: str, policy: RetryPolicy, http: HttpClient | None = None,
                          env: dict | None = None) -> FalImageGenerator:
    env = os.environ if env is None else env
    http = http or default_http()
    spec = catalog["image_models"][key]
    queue = FalQueue(http, env.get("FAL_KEY", ""), catalog["fal"]["queue_base_url"])
    return FalImageGenerator(queue, http, spec, policy)
