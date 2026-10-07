"""Ghi vết các bước agent ra file để xem lại: LLM đề xuất gì, policy quyết định ra sao, tool trả về gì."""

import json
import os
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(os.getenv("AGENT_TRACE_FILE") or Path(__file__).resolve().parent.parent / "logs" / "agent_trace.log")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def log(title: str, body=None) -> None:
    """Ghi một dòng trace. Lỗi ghi file không bao giờ được làm hỏng agent."""
    line = f"[{datetime.now():%H:%M:%S}] {title}"
    if body is not None:
        line += f"\n    {body if isinstance(body, str) else _dump(body)}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def start_session(mode: str, model: str) -> None:
    log(f"{'=' * 20} PHIÊN MỚI ({mode}, model={model}) {'=' * 20}")


def log_llm_call(step: int, model: str, seconds: float, response) -> None:
    """Ghi một lượt gọi Gemini: token, các tool nó đề xuất, và text nếu có."""
    usage = getattr(response, "usage_metadata", None)
    tokens = (f"prompt={usage.prompt_token_count} output={usage.candidates_token_count} "
              f"total={usage.total_token_count}") if usage else "n/a"
    calls = [{"name": c.name, "args": dict(c.args or {})} for c in (response.function_calls or [])]
    content = response.candidates[0].content if response.candidates else None
    text = " ".join(p.text for p in (content.parts or []) if p.text) if content else ""
    log(f"LLM #{step} ({seconds:.2f}s, tokens: {tokens})")
    if calls:
        log("  LLM đề xuất gọi tool", calls)
    if text and calls:  # text của lượt cuối đã được ghi ở "TRẢ LỜI KHÁCH"
        log("  LLM nói kèm", text)
