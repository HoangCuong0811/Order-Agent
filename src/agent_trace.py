"""In từng bước chạy của agent ra console của backend, để xem lại đường đi của mỗi lượt hội thoại.

Một lượt được ghi đúng thứ tự hệ thống chạy:
nhận câu hỏi -> gọi LLM -> LLM trả về (tool call hoặc câu trả lời) -> policy kiểm tra -> thực thi tool
-> gửi kết quả tool lại cho LLM -> gọi LLM tổng hợp -> trả lời khách.

Dòng đầu của mỗi bước có dạng `[giờ] [phiên Lượt #số-bước +giây-từ-đầu-lượt] TIÊU ĐỀ`, các dòng sau
là chi tiết. Không ghi file; xem trong log của backend.
"""

import json
import threading
import time
from datetime import datetime

VALUE_LIMIT = 1500  # cắt giá trị dài để mỗi bước đọc được trong một màn hình
HISTORY_LINES = 6  # số message gần nhất của lịch sử được liệt kê khi gọi LLM

_print_lock = threading.Lock()  # nhiều phiên chạy song song, mỗi bước phải được in liền một khối


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _clip(text: str, limit: int = VALUE_LIMIT) -> str:
    return text if len(text) <= limit else f"{text[:limit]}... (cắt, dài {len(text)} ký tự)"


def dump(value, limit: int = VALUE_LIMIT) -> str:
    return _clip(json.dumps(value, ensure_ascii=False, default=str), limit)


def _format_body(body) -> str:
    """body là chuỗi, danh sách dòng, hoặc giá trị bất kỳ (ghi dưới dạng JSON). Mỗi dòng thụt vào 4 khoảng trắng."""
    if body is None:
        return ""
    items = body if isinstance(body, list) else [body]
    lines = []
    for item in items:
        text = item if isinstance(item, str) else dump(item)
        lines.extend(f"    {line}" for line in text.splitlines() or [""])
    return "\n".join(lines) + "\n"


def _write(text: str) -> None:
    try:
        with _print_lock:
            print(text, end="", flush=True)
    except (OSError, UnicodeEncodeError):
        pass  # console lỗi không bao giờ được làm hỏng agent


def event(title: str, body=None) -> None:
    """Sự kiện cấp server, không thuộc lượt hội thoại nào (khởi động, tin nhắn bị API từ chối...)."""
    _write(f"[{_now()}] {title}\n{_format_body(body)}")


class Tracer:
    """Ghi vết cho một phiên. Mỗi lượt hội thoại có số lượt riêng và các bước đánh số từ #01."""

    def __init__(self, session_id: str):
        self.tag = str(session_id)[:8]
        self.turn = 0
        self._n = 0
        self._t0 = time.perf_counter()

    def note(self, title: str, body=None) -> None:
        """Ghi một dòng cấp phiên, ngoài các lượt (mở phiên...)."""
        _write(f"[{_now()}] [{self.tag}] {title}\n{_format_body(body)}")

    def begin_turn(self) -> None:
        self.turn += 1
        self._n = 0
        self._t0 = time.perf_counter()
        _write(f"[{_now()}] [{self.tag} L{self.turn}] {'=' * 10} LƯỢT {self.turn} BẮT ĐẦU {'=' * 10}\n")

    def step(self, title: str, body=None) -> None:
        self._n += 1
        elapsed = time.perf_counter() - self._t0
        _write(f"[{_now()}] [{self.tag} L{self.turn} #{self._n:02d} +{elapsed:.2f}s] {title}\n{_format_body(body)}")

    def end_turn(self) -> None:
        _write(f"[{_now()}] [{self.tag} L{self.turn}] {'=' * 10} LƯỢT {self.turn} KẾT THÚC {'=' * 10}\n\n")


def _describe_part(part, limit: int = VALUE_LIMIT) -> str:
    call = getattr(part, "function_call", None)
    if call:
        return f"function_call {call.name}({dump(dict(call.args or {}), limit)})"
    reply = getattr(part, "function_response", None)
    if reply:
        return f"function_response {reply.name} -> {dump(reply.response, limit)}"
    text = getattr(part, "text", None)
    if text:
        label = "thought" if getattr(part, "thought", False) else "text"
        return f'{label} "{_clip(text, limit)}"'
    return "part khác"


def describe_contents(contents) -> list[str]:
    """Liệt kê lịch sử đang gửi cho LLM: mỗi message một dòng, chỉ các message gần nhất."""
    lines = []
    shown = contents[-HISTORY_LINES:]
    skipped = len(contents) - len(shown)
    if skipped:
        lines.append(f"... {skipped} message cũ hơn không liệt kê")
    for i, content in enumerate(shown, start=skipped):
        parts = "; ".join(_describe_part(p, 200) for p in (content.parts or []))
        lines.append(f"[{i}] {content.role}: {parts}")
    return lines


def describe_response(response) -> tuple[str, str, list[str]]:
    """Trả về (token, lý do kết thúc, các phần LLM trả về) của một lượt gọi Gemini."""
    usage = getattr(response, "usage_metadata", None)
    tokens = (f"prompt={usage.prompt_token_count} output={usage.candidates_token_count} "
              f"total={usage.total_token_count}") if usage else "n/a"
    candidate = response.candidates[0] if response.candidates else None
    finish = getattr(candidate, "finish_reason", None)
    finish = getattr(finish, "name", finish) or "n/a"
    content = candidate.content if candidate else None
    parts = [_describe_part(p) for p in (content.parts or [])] if content else []
    return tokens, str(finish), parts
