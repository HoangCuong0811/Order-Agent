"""Vòng lặp agent: Gemini đề xuất gọi tool, policy quyết định, người duyệt khi cần.

Agent không chặn để chờ người duyệt. Khi một tool cần duyệt, lượt hội thoại tạm dừng và
`chat` trả về TurnResult(status="pending_approval"); `resolve_approval` chạy tiếp đúng từ chỗ đó.
"""

import json
import os
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import types

import policy
import tools
import agent_trace as trace

MAX_TOOL_CALLS = 5  # giới hạn số lượt gọi Gemini cho mỗi tin nhắn của khách

REPLY = "reply"
PENDING_APPROVAL = "pending_approval"

SYSTEM_PROMPT = """You are a customer support agent for an online shop.
Reply in the same language the customer uses.

Rules:
- Always use tools to look up real order data. Never invent order information.
- If the customer asks about an order but gives no order ID (format ORD-1234), ask for it.
- To cancel or refund, call the tool. The system decides whether it runs automatically,
  needs human approval, or is blocked. Do not decide this yourself and do not argue the rules.
- If a tool result says "blocked", explain the reason to the customer. Do not try workarounds.
- If a tool result says "rejected_by_human", tell the customer the request was not approved.
- For anything outside your tools or authority (delete account, complaints, compensation,
  legal issues), or if you are unsure, call escalate_to_human.

End EVERY reply with exactly these three lines:
Tóm tắt: <the customer's issue / the order situation>
Đề xuất: <the recommended next action>
Trạng thái: <one of: ĐÃ XỬ LÝ | CHỜ DUYỆT | ĐÃ ESCALATE | CẦN THÊM THÔNG TIN>"""

ERROR_REPLY = "Xin lỗi, hệ thống đang gặp lỗi ({error}). Vui lòng thử lại sau."


class ApprovalStateError(Exception):
    """Gọi sai thứ tự: gửi tin khi đang chờ duyệt, hoặc duyệt khi không có yêu cầu nào."""


@dataclass
class ToolEvent:
    """Một lần agent gọi tool trong lượt này, để UI hiển thị quyết định của guardrail."""
    tool: str
    args: dict
    level: str  # AUTO | NEEDS_APPROVAL | BLOCKED
    reason: str
    outcome: str  # executed | blocked | awaiting_approval | approved | rejected | error


@dataclass
class PendingApproval:
    tool: str
    args: dict
    reason: str


@dataclass
class TurnResult:
    status: str  # REPLY | PENDING_APPROVAL
    reply: str | None = None
    approval: PendingApproval | None = None
    tool_events: list[ToolEvent] = field(default_factory=list)


def make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def model_name() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")


class SupportAgent:
    """Một hội thoại. Không thread-safe: tầng gọi phải tuần tự hoá các lượt của cùng một phiên."""

    def __init__(self, client: genai.Client):
        self.client = client
        self.history: list[types.Content] = []
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=list(tools.TOOLS.values()),
            # Tắt tự động gọi tool để mình chèn policy vào giữa.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        # Trạng thái của lượt đang chạy dở (chỉ có ý nghĩa khi đang chờ duyệt).
        self._checkpoint = 0
        self._step = 0
        self._message = ""
        self._calls: list = []  # các function call Gemini đề xuất trong bước hiện tại
        self._results: list[types.Part] = []  # kết quả đã có, theo đúng thứ tự _calls
        self._pending: PendingApproval | None = None
        self._events: list[ToolEvent] = []

    @property
    def pending(self) -> PendingApproval | None:
        return self._pending

    def chat(self, message: str) -> TurnResult:
        if self._pending:
            raise ApprovalStateError("đang chờ nhân viên duyệt một yêu cầu")
        return self._guarded(lambda: self._start_turn(message))

    def resolve_approval(self, approved: bool) -> TurnResult:
        if not self._pending:
            raise ApprovalStateError("không có yêu cầu nào đang chờ duyệt")
        return self._guarded(lambda: self._resume(approved))

    # --- điều khiển lượt hội thoại ---

    def _guarded(self, run) -> TurnResult:
        self._events = []
        try:
            return run()
        except Exception as e:
            self._rollback()  # bỏ lượt dở để lịch sử không bị hỏng
            trace.log(f"LỖI {type(e).__name__}", str(e))
            return self._reply(ERROR_REPLY.format(error=type(e).__name__))

    def _start_turn(self, message: str) -> TurnResult:
        self._checkpoint = len(self.history)
        self._step = 0
        self._message = message
        self.history.append(types.Content(role="user", parts=[types.Part(text=message)]))
        trace.log("-" * 60)
        trace.log("KHÁCH", message)
        return self._advance()

    def _advance(self) -> TurnResult:
        """Gọi Gemini lặp lại cho tới khi có câu trả lời, cần duyệt, hoặc hết lượt."""
        while self._step < MAX_TOOL_CALLS:
            self._step += 1
            started = time.perf_counter()
            response = self.client.models.generate_content(
                model=model_name(), contents=self.history, config=self.config
            )
            trace.log_llm_call(self._step, model_name(), time.perf_counter() - started, response)
            if not response.candidates or response.candidates[0].content is None:
                self._rollback()
                return self._escalate("Gemini không trả về nội dung.")
            self.history.append(response.candidates[0].content)

            calls = response.function_calls
            if not calls:
                answer = response.text or "Xin lỗi, tôi chưa có câu trả lời phù hợp."
                trace.log("TRẢ LỜI KHÁCH", answer)
                return self._reply(answer)

            self._calls, self._results = list(calls), []
            paused = self._run_calls()
            if paused:
                return paused

        # Quá số lượt cho phép: dừng và giao cho người.
        self._rollback()
        return self._escalate(f"Agent vượt quá {MAX_TOOL_CALLS} lượt gọi tool.")

    def _run_calls(self) -> TurnResult | None:
        """Chạy các tool Gemini đề xuất. Trả về TurnResult nếu phải dừng chờ duyệt, ngược lại None."""
        while len(self._results) < len(self._calls):
            call = self._calls[len(self._results)]
            args = dict(call.args or {})
            result = self._handle_call(call.name, args)
            if result is None:
                return TurnResult(PENDING_APPROVAL, approval=self._pending, tool_events=self._events)
            self._collect(call.name, result)
        self.history.append(types.Content(role="user", parts=self._results))
        self._calls, self._results = [], []
        return None

    def _resume(self, approved: bool) -> TurnResult:
        pending, self._pending = self._pending, None
        trace.log(f"  Người duyệt: {'ĐỒNG Ý' if approved else 'TỪ CHỐI'}")
        if not approved:
            self._event(pending.tool, pending.args, policy.NEEDS_APPROVAL, pending.reason, "rejected")
            result = {"status": "rejected_by_human", "reason": "nhân viên không duyệt yêu cầu này"}
        else:
            # Dữ liệu có thể đã đổi từ lúc xin duyệt (phiên khác huỷ cùng đơn...), nên kiểm lại.
            order, _ = tools.find_order(pending.args.get("order_id"))
            decision = policy.evaluate(pending.tool, pending.args, order)
            if decision.level == policy.BLOCKED:
                self._event(pending.tool, pending.args, decision.level, decision.reason, "blocked")
                result = {"status": "blocked", "reason": decision.reason}
            else:
                result = self._execute(pending.tool, pending.args, policy.NEEDS_APPROVAL, pending.reason)
                result["approved_by_human"] = True
        self._collect(pending.tool, result)

        paused = self._run_calls()
        return paused or self._advance()

    # --- thực thi tool ---

    def _handle_call(self, name: str, args: dict) -> dict | None:
        """Trả về kết quả tool để gửi lại cho Gemini, hoặc None nếu cần dừng chờ duyệt."""
        if name not in tools.TOOLS:
            trace.log(f"  TOOL {name}: không tồn tại", args)
            self._event(name, args, policy.BLOCKED, "tool không tồn tại", "blocked")
            return {"ok": False, "error": f"unknown tool: {name}"}

        order, _ = tools.find_order(args.get("order_id"))
        decision = policy.evaluate(name, args, order)
        print(f"  [tool] {name}({json.dumps(args, ensure_ascii=False)}) -> {decision.level}")
        trace.log(f"  TOOL {name}({json.dumps(args, ensure_ascii=False)})")
        trace.log(f"  Policy: {decision.level}", decision.reason)

        if decision.level == policy.BLOCKED:
            self._event(name, args, decision.level, decision.reason, "blocked")
            return {"status": "blocked", "reason": decision.reason}
        if decision.level == policy.NEEDS_APPROVAL:
            self._pending = PendingApproval(name, args, decision.reason)
            self._event(name, args, decision.level, decision.reason, "awaiting_approval")
            return None
        return self._execute(name, args, decision.level, decision.reason)

    def _execute(self, name: str, args: dict, level: str, reason: str) -> dict:
        outcome = "executed" if level == policy.AUTO else "approved"
        try:
            result = tools.TOOLS[name](**args)
        except TypeError:
            result, outcome = {"ok": False, "error": "invalid or missing arguments"}, "error"
        except Exception as e:
            result, outcome = {"ok": False, "error": f"tool failed: {type(e).__name__}"}, "error"
        self._event(name, args, level, reason, outcome)
        return result

    def _collect(self, name: str, result: dict) -> None:
        trace.log(f"  Kết quả tool gửi lại cho LLM: {name}", result)
        self._results.append(types.Part.from_function_response(name=name, response=result))

    # --- tiện ích ---

    def _event(self, tool: str, args: dict, level: str, reason: str, outcome: str) -> None:
        self._events.append(ToolEvent(tool, args, level, reason, outcome))

    def _rollback(self) -> None:
        self.history = self.history[:self._checkpoint]
        self._calls, self._results, self._pending = [], [], None

    def _reply(self, text: str) -> TurnResult:
        return TurnResult(REPLY, reply=text, tool_events=self._events)

    def _escalate(self, summary: str) -> TurnResult:
        summary = f"{summary} Tin nhắn khách: {self._message[:200]}"
        trace.log("ESCALATE (do hệ thống, không phải LLM)", summary)
        tools.escalate_to_human(summary)
        return self._reply("Yêu cầu này cần nhân viên hỗ trợ xử lý, tôi đã chuyển cho họ.\n"
                           "Tóm tắt: agent không thể tự xử lý yêu cầu này\n"
                           "Đề xuất: chờ nhân viên liên hệ\n"
                           "Trạng thái: ĐÃ ESCALATE")
