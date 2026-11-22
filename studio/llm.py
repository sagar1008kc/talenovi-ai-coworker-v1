"""Claude API calls for research, creative decisions and reviews (paid; budget-reserved).

Uses the official `anthropic` SDK. Structured outputs use output_config.format (JSON schema);
research uses the server-side web search tool so sources are real URLs returned by search,
which lets the pipeline verify that every cited source was actually seen.
"""
from __future__ import annotations

import base64
from io import BytesIO
import json
import logging
from pathlib import Path
from typing import Any, TypeVar

from PIL import Image
from pydantic import BaseModel, ValidationError

from studio.budget import CostLedger
from studio.models import (FactReview, FrameReview, ResearchBrief, ShortScript, ShotPlans,
                           TopicCandidates, VideoScript)

log = logging.getLogger("studio.llm")
T = TypeVar("T", bound=BaseModel)

UNSUPPORTED_SCHEMA_KEYS = {"title", "default", "minimum", "maximum", "exclusiveMinimum",
                           "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems",
                           "multipleOf", "pattern", "uniqueItems"}

EDITORIAL_SYSTEM = """You are the head writer and researcher for an original, high-value video channel.
Rules you never break:
- Write original narration in your own words. Never copy, translate or closely paraphrase existing
  videos, articles, scripts or social posts. Research informs facts, never wording or structure.
- Every factual claim must be supported by a source you actually found. If evidence is weak,
  say so or leave the claim out. No invented statistics, quotes, dates or people.
- Honest hooks only: the opening must be true and paid off by the video. No clickbait deception.
- Broadly family-safe: no profanity, sexual content, gore, hate or shock for its own sake.
- Give the viewer real educational or entertainment value; avoid filler and templated phrasing.
- Do not depict real private individuals, and never imitate real creators, celebrities or brands."""


class LLMError(RuntimeError):
    pass


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema adapted to Claude structured-output limits."""
    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in ("properties", "$defs"):  # keys here are field/definition NAMES, keep all
                    out[k] = {name: clean(sub) for name, sub in v.items()}
                elif k not in UNSUPPORTED_SCHEMA_KEYS:
                    out[k] = clean(v)
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node
    return clean(model.model_json_schema())


def _block_text(content: list) -> str:
    return "".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text")


def _search_urls(content: list) -> set[str]:
    urls: set[str] = set()
    for block in content:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        results = getattr(block, "content", None)
        if isinstance(results, list):  # an error result is an object, not a list
            urls.update(u for u in (getattr(r, "url", None) for r in results) if u)
    return urls


def _jpeg_b64(path: Path, max_side: int = 768) -> str:
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode("ascii")


class ClaudeStudio:
    provider = "anthropic"

    def __init__(self, client: Any, model: str, catalog_claude: dict, effort: str = "high",
                 channel_brief: str = ""):
        prices = catalog_claude["prices_usd_per_mtok"]
        if model not in prices:
            raise LLMError(f"No verified price for {model}; add it to config/providers.json first")
        self.client, self.model, self.effort = client, model, effort
        self.price_in = prices[model]["input"] / 1_000_000
        self.price_out = prices[model]["output"] / 1_000_000
        self.search_usd = catalog_claude["web_search_usd_per_search"]
        self.search_type = catalog_claude["web_search_tool_type"]
        self.fallback_beta = catalog_claude["fallback_beta"]
        self.system = EDITORIAL_SYSTEM + ("\n\nCHANNEL BRIEF:\n" + channel_brief if channel_brief else "")

    # ------------------------------------------------------------------ core call
    def _call(self, ledger: CostLedger, item: str, content: Any, schema: type[T],
              searches: int = 0, max_tokens: int = 16000, effort: str | None = None) -> tuple[T, set[str]]:
        prompt_chars = len(self.system) + len(json.dumps(content, default=str))
        worst_input = prompt_chars / 3.0 + searches * 25_000  # search results add input tokens
        estimate = worst_input * self.price_in + max_tokens * self.price_out + searches * self.search_usd
        entry = ledger.reserve(self.provider, self.model, item, estimate)
        tools = ([{"type": self.search_type, "name": "web_search", "max_uses": searches}]
                 if searches else [])
        messages: list[dict] = [{"role": "user", "content": content}]
        in_tok = out_tok = search_count = 0
        seen: set[str] = set()
        try:
            for _ in range(5):  # pause_turn continuations
                kwargs: dict[str, Any] = dict(
                    model=self.model, max_tokens=max_tokens, system=self.system, messages=messages,
                    betas=[self.fallback_beta],
                    output_config={"effort": effort or self.effort,
                                   "format": {"type": "json_schema", "schema": strict_schema(schema)}},
                    # Server-side refusal fallback; sent as extra_body so older SDK pins still work.
                    extra_body={"fallbacks": "default"},
                )
                if tools:
                    kwargs["tools"] = tools
                response = self.client.beta.messages.create(**kwargs)
                usage = response.usage
                in_tok += (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                out_tok += getattr(usage, "output_tokens", 0) or 0
                stu = getattr(usage, "server_tool_use", None)
                search_count += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
                seen |= _search_urls(response.content)
                if response.stop_reason == "pause_turn":
                    messages = [messages[0], {"role": "assistant", "content": response.content}]
                    continue
                break
            else:
                raise LLMError(f"{item}: too many pause_turn continuations")
            if response.stop_reason == "refusal":
                raise LLMError(f"{item}: model refused ({getattr(response, 'stop_details', None)})")
            if response.stop_reason == "max_tokens":
                raise LLMError(f"{item}: output truncated at max_tokens={max_tokens}")
            try:
                parsed = schema.model_validate(json.loads(_block_text(response.content)))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise LLMError(f"{item}: invalid structured output: {exc}") from exc
        except Exception as exc:
            ledger.settle(entry, "possibly_billed", error=str(exc)[:300])
            raise
        actual = in_tok * self.price_in + out_tok * self.price_out + search_count * self.search_usd
        ledger.settle(entry, "ok", actual_usd=actual, input_tokens=in_tok, output_tokens=out_tok,
                      web_searches=search_count)
        log.info("claude call", extra={"event": "llm_call", "item": item, "usd": round(actual, 4),
                                       "input_tokens": in_tok, "output_tokens": out_tok,
                                       "searches": search_count})
        return parsed, seen

    # ------------------------------------------------------------------ pipeline steps
    def discover_topics(self, ledger: CostLedger, niche: str, audience: str, avoid: list[str],
                        count: int = 6) -> TopicCandidates:
        prompt = (
            f"Niche: {niche}\nAudience: {audience}\n"
            f"Propose {count} distinct video topics with lasting value for this audience. Use web search "
            "to gauge what people are curious about now and to make sure each topic is factually "
            "researchable from reputable sources. Each topic needs a specific, non-generic angle.\n"
            "Do NOT propose anything overlapping these previously used topics:\n- "
            + "\n- ".join(avoid[-80:] or ["(none yet)"])
        )
        result, _ = self._call(ledger, "topic discovery", prompt, TopicCandidates, searches=4,
                               max_tokens=6000, effort="medium")
        return result

    def research(self, ledger: CostLedger, title: str, angle: str, max_searches: int) -> tuple[ResearchBrief, set[str]]:
        prompt = (
            f"Research this video topic thoroughly.\nTitle: {title}\nAngle: {angle}\n"
            "Return 10-25 atomic, checkable claims the video could use. For each claim list the exact "
            "URLs (from your search results) that support it; prefer primary or reputable sources. "
            "Mark confidence honestly. List open questions where evidence is disputed or thin."
        )
        return self._call(ledger, "research", prompt, ResearchBrief, searches=max_searches,
                          max_tokens=12000)

    def write_script(self, ledger: CostLedger, title: str, angle: str, brief: ResearchBrief,
                     words: tuple[int, int], feedback: str = "") -> VideoScript:
        prompt = (
            f"Write the narration for a long-form 16:9 video.\nWorking title: {title}\nAngle: {angle}\n"
            f"Length: {words[0]}-{words[1]} spoken words in total (this is strict).\n"
            "Structure: a true, concrete hook in the first two sentences; 5-9 sections that build "
            "understanding with varied rhythm, concrete examples and a clear payoff; a satisfying "
            "conclusion. Write for the ear: natural sentences, no headings or stage directions inside "
            "narration, no lists read aloud. Only use facts from the research claims and reference "
            "their claim_ids per section. thumbnail_text: 2-5 honest, intriguing words.\n"
            f"RESEARCH (JSON):\n{brief.model_dump_json()}\n"
            + (f"\nREVISION FEEDBACK TO FIX:\n{feedback}\n" if feedback else "")
        )
        result, _ = self._call(ledger, "long-form script", prompt, VideoScript, max_tokens=16000)
        return result

    def write_short(self, ledger: CostLedger, script: VideoScript, brief: ResearchBrief,
                    words: tuple[int, int], feedback: str = "") -> ShortScript:
        prompt = (
            "Write a STANDALONE vertical Short related to this long-form video. It must make sense "
            "without the long video: one surprising, true idea with a hook in the first sentence and "
            f"a satisfying ending. Length: {words[0]}-{words[1]} spoken words (strict). Fast, clear "
            "sentences. Do not just compress the whole long script; pick one strong thread. Use only "
            "facts from the research claims and list their claim_ids.\n"
            f"LONG-FORM TITLE: {script.title}\nRESEARCH (JSON):\n{brief.model_dump_json()}\n"
            + (f"\nREVISION FEEDBACK TO FIX:\n{feedback}\n" if feedback else "")
        )
        result, _ = self._call(ledger, "short script", prompt, ShortScript, max_tokens=4000)
        return result

    def review_facts(self, ledger: CostLedger, script: VideoScript, short: ShortScript,
                     brief: ResearchBrief) -> FactReview:
        prompt = (
            "Act as an independent fact-checker. Check every factual statement in both scripts against "
            "the research claims and their sources; use web search to double-check anything doubtful. "
            "Flag claims that are unsupported, overstated, outdated or misattributed. verdict=pass only "
            "if there are no major issues.\n"
            f"RESEARCH:\n{brief.model_dump_json()}\nLONG SCRIPT:\n{script.model_dump_json()}\n"
            f"SHORT SCRIPT:\n{short.model_dump_json()}"
        )
        result, _ = self._call(ledger, "fact review", prompt, FactReview, searches=3, max_tokens=8000)
        return result

    def plan_shots(self, ledger: CostLedger, title: str, style: str, aspect: str,
                   chunks: list[tuple[str, str]], motion_slots: int) -> ShotPlans:
        listing = "\n".join(f"{sid}: {text}" for sid, text in chunks)
        prompt = (
            f"Plan visuals for a {aspect} video titled '{title}'. Visual style for EVERY shot: {style}.\n"
            "For each narration chunk below, return exactly one shot with the same shot_id. "
            "visual_prompt: a detailed, original, photorealistic or cinematic image description that "
            "illustrates the narration literally and keeps recurring subjects, palette and era "
            "consistent; no text, letters, logos, watermarks or real identifiable people. Vary framing "
            "(wide/medium/close/detail/aerial) so consecutive shots never look alike. "
            f"wants_motion=true only for at most {motion_slots} shots where real movement adds meaning "
            "(e.g. a process, weather, crowds, machinery); motion_prompt then describes the movement. "
            "camera: the move for the still/clip.\n" + listing
        )
        result, _ = self._call(ledger, f"shot plan {chunks[0][0]}", prompt, ShotPlans,
                               max_tokens=12000, effort="medium")
        ids = [s.shot_id for s in result.shots]
        if ids != [sid for sid, _ in chunks]:
            raise LLMError(f"Shot plan IDs mismatch: expected {len(chunks)} shots")
        return result

    def review_frames(self, ledger: CostLedger, title: str, frames: list[Path]) -> FrameReview:
        content: list[dict] = []
        for frame in frames:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": _jpeg_b64(frame)}})
        content.append({"type": "text", "text": (
            f"These are evenly sampled frames from the finished video '{title}'. Rate visual quality "
            "and style consistency 1-10. List concrete defects: garbled text or letters, distorted "
            "hands/faces, watermarks, logos, near-duplicate frames, off-topic imagery, unsafe content. "
            "verdict=needs_review if any defect would embarrass a professional channel.")})
        result, _ = self._call(ledger, "frame review", content, FrameReview, max_tokens=3000,
                               effort="medium")
        return result
