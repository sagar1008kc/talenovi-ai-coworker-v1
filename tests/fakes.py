"""Offline test doubles: scripted HTTP responses and a scripted Anthropic client. No network."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from studio.budget import BudgetCaps, CostLedger, MonthlySpend, RunSpend

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


class Resp:
    def __init__(self, status: int = 200, data=None, body: bytes = b"", text: str = ""):
        self.status_code, self._data, self._body = status, data, body
        self.text = text or (json.dumps(data) if data is not None else "")
        self.headers: dict = {}

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_content(self, chunk_size: int = 1):
        yield self._body


class FakeHttp:
    """routes: list of (METHOD, url_substring, response | [responses] | callable(kw)->response)."""

    def __init__(self, routes):
        self.routes = [list(r) for r in routes]
        self.calls: list[tuple[str, str, dict]] = []

    def _handle(self, method: str, url: str, **kw):
        self.calls.append((method, url, kw))
        for route in self.routes:
            if route[0] == method and route[1] in url:
                target = route[2]
                if isinstance(target, list):
                    return target.pop(0) if len(target) > 1 else target[0]
                return target(kw) if callable(target) else target
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, **kw):
        return self._handle("POST", url, **kw)

    def put(self, url, **kw):
        return self._handle("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._handle("DELETE", url, **kw)

    def count(self, method: str, fragment: str = "") -> int:
        return sum(1 for m, u, _ in self.calls if m == method and fragment in u)


def make_ledger(tmp: Path, video=50.0, run=50.0, month=500.0) -> CostLedger:
    return CostLedger(tmp / "cost_report.json", BudgetCaps(max_usd_per_video=video, max_usd_per_run=run,
                                                           max_usd_per_month=month), RunSpend(), MonthlySpend(tmp))


def claude_response(payload: dict | None = None, *, urls=(), stop="end_turn", input_tokens=1000,
                    output_tokens=500, searches=0, text: str | None = None):
    content = []
    if urls:
        content.append(SimpleNamespace(type="web_search_tool_result",
                                       content=[SimpleNamespace(type="web_search_result", url=u) for u in urls]))
    if text is not None or payload is not None:
        content.append(SimpleNamespace(type="text", text=text if text is not None else json.dumps(payload)))
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                            cache_creation_input_tokens=0,
                            server_tool_use=SimpleNamespace(web_search_requests=searches))
    return SimpleNamespace(content=content, usage=usage, stop_reason=stop, stop_details=None)


class FakeAnthropic:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)
