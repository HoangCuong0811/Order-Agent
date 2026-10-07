"""Vòng lặp agent: Gemini đề xuất gọi tool, policy quyết định, người duyệt khi cần.

Agent không chặn để chờ người duyệt. Khi một tool cần duyệt, lượt hội thoại tạm dừng và
`chat` trả về TurnResult(status="pending_approval"); `resolve_approval` chạy tiếp đúng từ chỗ đó.
"""

import os
import time
import traceback
from dataclasses import dataclass, field

from google import genai
from google.genai import types

import policy
import tools
import agent_trace as trace

MAX_TOOL_CALLS = 5  # giới hạn số lượt gọi Gemini cho mỗi tin nhắn của khách

REPLY = "reply"
PENDING_APPROVAL = "pending_approval"

RETURN_TOOL = "return_order"  # chỉ agent dùng (khách gửi qua UI), không khai báo cho LLM

SYSTEM_PROMPT = """You are a customer support agent for an online shop.
Reply in the same language the customer uses.

Rules:
- Always use tools to look up real order data. Never invent order information.
- If the customer asks about an order but gives no order ID (format ORD-1234), ask for it.
- To cancel or refund, call the tool. The system decides whether it runs automatically,
  needs human approval, or is blocked. Do not decide this yourself and do not argue the rules.
- If a tool result says "blocked", explain the reason to the customer. Do not try workarounds.
- If a tool result says "rejected_by_human", tell the customer the request was not approved and
  give the staff member's reason from the "reason" field. Do not invent other reasons.
- If a cancel is blocked because the order was already delivered and the reason mentions a return
  request, explain that the order cannot be cancelled and suggest the customer submit a return request.
  The system shows the customer a form for it; do not ask them to type the return details in chat.
- For anything outside your tools or authority (delete account, complaints, compensation,
  legal issues), or if you are unsure, call escalate_to_human.

End EVERY reply with exactly these three lines:
Tóm tắt: <the customer's issue / the order situation>
Đề xuất: <the recommended next action>
Trạng thái: <one of: ĐÃ XỬ LÝ | CHỜ DUYỆT | ĐÃ ESCALATE | CẦN THÊM THÔNG TIN>"""

ERROR_REPLY = "Xin lỗi, hệ thống đang gặp lỗi ({error}). Vui lòng thử lại sau."

# Thông báo kết quả hoàn hàng: mẫu cố định, không qua LLM (luôn đủ lý do, vẫn kết thúc bằng 3 dòng chuẩn).
RETURN_APPROVED_REPLY = (
    "Yêu cầu hoàn hàng cho đơn {order_id} đã được duyệt. Đơn chuyển sang trạng thái \"returned\" "
    "và số tiền {amount:.2f} sẽ được hoàn lại cho bạn.\n"
    "Tóm tắt: khách yêu cầu hoàn hàng đơn {order_id}, lý do: {reason}\n"
    "Đề xuất: không cần làm thêm gì, hãy liên hệ lại nếu cần hỗ trợ\n"
    "Trạng thái: ĐÃ XỬ LÝ")
RETURN_REJECTED_REPLY = (
    "Yêu cầu hoàn hàng cho đơn {order_id} không được duyệt.\n"
    "Lý do từ nhân viên: {staff_reason}\n"
    "Tóm tắt: yêu cầu hoàn hàng đơn {order_id} bị từ chối\n"
    "Đề xuất: liên hệ lại nếu bạn có thêm thông tin\n"
    "Trạng thái: ĐÃ XỬ LÝ")
RETURN_BLOCKED_REPLY = (
    "Không thể thực hiện hoàn hàng cho đơn {order_id} vì dữ liệu đơn đã thay đổi: {reason}.\n"
    "Tóm tắt: yêu cầu hoàn hàng đơn {order_id} không còn hợp lệ\n"
    "Đề xuất: kiểm tra lại trạng thái đơn hoặc liên hệ nhân viên\n"
    "Trạng thái: ĐÃ XỬ LÝ")


def _one_line(text: str) -> str:
    """Gộp khoảng trắng và xuống dòng, để lý do của người dùng không phá định dạng 3 dòng cuối của câu trả lời."""
    return " ".join((text or "").split())


class ApprovalStateError(Exception):
    """Gọi sai thứ tự: gửi tin khi đang chờ duyệt, hoặc duyệt khi không có yêu cầu nào."""


class ReturnNotAllowed(Exception):
    """Đơn không đủ điều kiện hoàn hàng (policy chặn); thông điệp là lý do để báo lại cho khách."""


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
    offer_return: str | None = None  # mã đơn nếu UI nên mời khách làm yêu cầu hoàn hàng


def make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def model_name() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")


class SupportAgent:
    """Một hội thoại. Không thread-safe: tầng gọi phải tuần tự hoá các lượt của cùng một phiên."""

    def __init__(self, client: genai.Client, session_id: str = "local"):
        self.client = client
        self.trace = trace.Tracer(session_id)
        self.history: list[types.Content] = []
        # Dữ liệu đơn riêng của phiên này (bản sao dữ liệu mẫu); tool chỉ sửa bản sao này.
        self.orders = tools.new_session_orders()
        self.tools = tools.make_tools(self.orders)
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=list(self.tools.values()),
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
        self._return_offer: str | None = None  # đặt khi huỷ đơn bị chặn vì đã giao, trả kèm câu trả lời cuối
        # Số liệu tổng kết của lượt, chỉ để in ở bước cuối.
        self._turn_started = 0.0
        self._pending_since = 0.0
        self._wait_seconds = 0.0
        self._tool_runs = 0
        self.trace.note("PHIÊN MỚI", f"model={model_name()}, tối đa {MAX_TOOL_CALLS} lượt gọi LLM cho mỗi tin nhắn")

    @property
    def pending(self) -> PendingApproval | None:
        return self._pending

    def chat(self, message: str) -> TurnResult:
        if self._pending:
            raise ApprovalStateError("đang chờ nhân viên duyệt một yêu cầu")
        return self._guarded(lambda: self._start_turn(message))

    def resolve_approval(self, approved: bool, reason: str = "") -> TurnResult:
        """Nhân viên quyết định yêu cầu đang chờ. Từ chối thì `reason` bắt buộc (API đã kiểm tra)."""
        if not self._pending:
            raise ApprovalStateError("không có yêu cầu nào đang chờ duyệt")
        reason = _one_line(reason)
        if not approved and not reason:
            raise ValueError("từ chối phải kèm lý do")
        if self._pending.tool == RETURN_TOOL:
            return self._guarded(lambda: self._resume_return(approved, reason))
        return self._guarded(lambda: self._resume(approved, reason))

    def request_return(self, order_id: str, video: str, reason: str) -> TurnResult:
        """Khách gửi yêu cầu hoàn hàng (từ form trên UI). Không qua LLM; luôn dừng chờ nhân viên duyệt."""
        if self._pending:
            raise ApprovalStateError("đang chờ nhân viên duyệt một yêu cầu")
        order, _ = tools.find_order(self.orders, order_id)
        decision = policy.evaluate_return(order)
        if decision.level == policy.BLOCKED:
            self.trace.note("YÊU CẦU HOÀN HÀNG BỊ TỪ CHỐI", f"{order_id}: {decision.reason}")
            raise ReturnNotAllowed(decision.reason)
        return self._guarded(lambda: self._start_return(order, decision, _one_line(video), _one_line(reason)))

    # --- điều khiển lượt hội thoại ---

    def _guarded(self, run) -> TurnResult:
        self._events = []
        try:
            return run()
        except Exception as e:
            self._rollback()  # bỏ lượt dở để lịch sử không bị hỏng
            self.trace.step(f"LỖI {type(e).__name__}", [
                str(e),
                "→ hoàn tác lịch sử hội thoại về trước lượt này, trả thông báo lỗi cho khách",
                traceback.format_exc().rstrip(),
            ])
            return self._reply(ERROR_REPLY.format(error=type(e).__name__))

    def _start_turn(self, message: str) -> TurnResult:
        self.trace.begin_turn()
        self.trace.step("NHẬN CÂU HỎI TỪ KHÁCH", [
            f'Nội dung: "{message}"',
            f"Lịch sử hội thoại đã có: {len(self.history)} message (chưa tính câu này)",
        ])
        self._checkpoint = len(self.history)
        self._step = 0
        self._return_offer = None
        self._message = message
        self._turn_started = time.perf_counter()
        self._wait_seconds = 0.0
        self._tool_runs = 0
        self.history.append(types.Content(role="user", parts=[types.Part(text=message)]))
        return self._advance()

    def _advance(self) -> TurnResult:
        """Gọi Gemini lặp lại cho tới khi có câu trả lời, cần duyệt, hoặc hết lượt."""
        while self._step < MAX_TOOL_CALLS:
            self._step += 1
            self.trace.step(f"GỌI LLM (lần {self._step}/{MAX_TOOL_CALLS})", [
                f"Model: {model_name()}",
                f"Tools khai báo cho LLM: {', '.join(self.tools)}",
                f"Gửi {len(self.history)} message lịch sử:",
                *[f"  {line}" for line in trace.describe_contents(self.history)],
            ])
            started = time.perf_counter()
            response = self.client.models.generate_content(
                model=model_name(), contents=self.history, config=self.config
            )
            seconds = time.perf_counter() - started
            tokens, finish, parts = trace.describe_response(response)

            calls = response.function_calls
            if not response.candidates or response.candidates[0].content is None:
                self.trace.step("LLM TRẢ VỀ RỖNG", [f"Thời gian gọi: {seconds:.2f}s", "→ hoàn tác lịch sử, chuyển cho nhân viên"])
                self._rollback()
                return self._escalate("Gemini không trả về nội dung.")
            self.trace.step("LLM TRẢ VỀ", [
                f"Thời gian gọi: {seconds:.2f}s · token: {tokens} · finish_reason: {finish}",
                *[f"  {line}" for line in parts],
                (f"→ LLM yêu cầu gọi {len(calls)} tool: {', '.join(c.name for c in calls)}"
                 if calls else
                 "→ LLM không gọi tool, đây là câu trả lời cuối"
                 + (" (tổng hợp từ kết quả tool ở các bước trước)" if self._tool_runs else " (trả lời trực tiếp)")),
            ])
            self.history.append(response.candidates[0].content)

            if not calls:
                return self._reply(response.text or "Xin lỗi, tôi chưa có câu trả lời phù hợp.",
                                   offer_return=self._return_offer)

            self._calls, self._results = list(calls), []
            paused = self._run_calls()
            if paused:
                return paused

        # Quá số lượt cho phép: dừng và giao cho người.
        self.trace.step("VƯỢT GIỚI HẠN SỐ LƯỢT GỌI LLM", f"Đã gọi LLM {MAX_TOOL_CALLS} lần mà chưa có câu trả lời cuối")
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
        self.trace.step("GỬI KẾT QUẢ TOOL LẠI CHO LLM", [
            *[f"{p.function_response.name} → {trace.dump(p.function_response.response)}" for p in self._results],
            "→ ghi vào lịch sử (role=user, function_response), rồi gọi LLM lần kế tiếp để tổng hợp thành câu trả lời",
        ])
        self.history.append(types.Content(role="user", parts=self._results))
        self._calls, self._results = [], []
        return None

    def _resume(self, approved: bool, reason: str = "") -> TurnResult:
        pending, self._pending = self._pending, None
        waited = time.perf_counter() - self._pending_since
        self._wait_seconds += waited
        self.trace.step(f"NHÂN VIÊN {'ĐỒNG Ý' if approved else 'TỪ CHỐI'}", [
            f"Yêu cầu: {pending.tool}({trace.dump(pending.args)})",
            f"Thời gian chờ duyệt: {waited:.1f}s",
            *([] if approved else [f"Lý do từ chối: {reason}"]),
            "→ kiểm tra lại policy trước khi chạy" if approved
            else "→ không chạy tool; trả {status: rejected_by_human, reason} cho LLM để báo khách",
        ])
        if not approved:
            self._event(pending.tool, pending.args, policy.NEEDS_APPROVAL, pending.reason, "rejected")
            result = {"status": "rejected_by_human", "reason": reason}
        else:
            # Kiểm lại cho chắc yêu cầu còn hợp lệ trên dữ liệu hiện tại của phiên.
            order, _ = tools.find_order(self.orders, pending.args.get("order_id"))
            decision = policy.evaluate(pending.tool, pending.args, order)
            self.trace.step(f"POLICY KIỂM TRA LẠI TOOL {pending.tool}", [
                f"Dữ liệu policy dựa vào: {self._order_snapshot(pending.args, order)}",
                f"Quyết định: {decision.level} — {decision.reason}",
                "→ dữ liệu đã đổi, không chạy tool" if decision.level == policy.BLOCKED
                else "→ vẫn hợp lệ, chạy tool vì nhân viên đã duyệt",
            ])
            if decision.level == policy.BLOCKED:
                self._event(pending.tool, pending.args, decision.level, decision.reason, "blocked")
                result = {"status": "blocked", "reason": decision.reason}
            else:
                result = self._execute(pending.tool, pending.args, policy.NEEDS_APPROVAL, pending.reason)
                result["approved_by_human"] = True
        self._collect(pending.tool, result)

        paused = self._run_calls()
        return paused or self._advance()

    # --- hoàn hàng (không qua LLM) ---

    def _start_return(self, order: dict, decision: policy.Decision, video: str, reason: str) -> TurnResult:
        """Khách đã gửi form hoàn hàng và policy cho phép: dừng chờ nhân viên duyệt."""
        self.trace.begin_turn()
        self._checkpoint = len(self.history)
        self._step = 0
        self._return_offer = None
        self._turn_started = time.perf_counter()
        self._wait_seconds = 0.0
        self._tool_runs = 0
        args = {"order_id": order["order_id"], "amount": policy.refundable_amount(order),
                "video": video, "reason": reason}
        self.trace.step("KHÁCH GỬI YÊU CẦU HOÀN HÀNG", [
            f"Dữ liệu policy dựa vào: {self._order_snapshot(args, order)}",
            f"Video (mô phỏng, không upload): {video}",
            f"Lý do hoàn: {reason}",
            f"Quyết định: {decision.level} — {decision.reason}",
        ])
        self._pending = PendingApproval(RETURN_TOOL, args, decision.reason)
        self._pending_since = time.perf_counter()
        self._event(RETURN_TOOL, args, decision.level, decision.reason, "awaiting_approval")
        self.trace.step("TẠM DỪNG CHỜ NHÂN VIÊN DUYỆT", [
            f"Yêu cầu: {RETURN_TOOL}({trace.dump(args)})",
            "→ API trả status=pending_approval; nhân viên gọi /approval, không qua LLM",
        ])
        return TurnResult(PENDING_APPROVAL, approval=self._pending, tool_events=self._events)

    def _resume_return(self, approved: bool, reason: str) -> TurnResult:
        pending, self._pending = self._pending, None
        args = pending.args
        order_id = args["order_id"]
        waited = time.perf_counter() - self._pending_since
        self._wait_seconds += waited
        self.trace.step(f"NHÂN VIÊN {'ĐỒNG Ý' if approved else 'TỪ CHỐI'} HOÀN HÀNG", [
            f"Yêu cầu: {RETURN_TOOL}({trace.dump(args)})",
            f"Thời gian chờ duyệt: {waited:.1f}s",
            *([] if approved else [f"Lý do từ chối: {reason}"]),
            "→ kiểm tra lại policy trước khi chạy" if approved else "→ không chạy; báo khách bằng mẫu cố định",
        ])
        if not approved:
            self._event(RETURN_TOOL, args, policy.NEEDS_APPROVAL, pending.reason, "rejected")
            text = RETURN_REJECTED_REPLY.format(order_id=order_id, staff_reason=reason)
        else:
            # Kiểm lại cho chắc yêu cầu còn hợp lệ trên dữ liệu hiện tại của phiên.
            order, _ = tools.find_order(self.orders, order_id)
            decision = policy.evaluate_return(order)
            self.trace.step("POLICY KIỂM TRA LẠI HOÀN HÀNG", [
                f"Dữ liệu policy dựa vào: {self._order_snapshot(args, order)}",
                f"Quyết định: {decision.level} — {decision.reason}",
                "→ dữ liệu đã đổi, không chạy" if decision.level == policy.BLOCKED
                else "→ vẫn hợp lệ, hoàn hàng và hoàn tiền vì nhân viên đã duyệt",
            ])
            if decision.level == policy.BLOCKED:
                self._event(RETURN_TOOL, args, decision.level, decision.reason, "blocked")
                text = RETURN_BLOCKED_REPLY.format(order_id=order_id, reason=decision.reason)
            else:
                result = tools.return_order(self.orders, order_id, args["video"], args["reason"])
                self._tool_runs += 1
                self._event(RETURN_TOOL, args, policy.NEEDS_APPROVAL, pending.reason, "approved")
                self.trace.step(f"THỰC THI {RETURN_TOOL} (đã được nhân viên duyệt)", [
                    f"Kết quả: {trace.dump(result)}",
                ])
                text = RETURN_APPROVED_REPLY.format(order_id=order_id, amount=result["refunded"],
                                                    reason=args["reason"])
        # Ghi vào lịch sử để LLM nắm ngữ cảnh nếu khách hỏi tiếp (trước đó là message role=model).
        self.history.append(types.Content(role="user", parts=[types.Part(text=(
            f"[Hệ thống] Khách đã gửi yêu cầu hoàn hàng cho đơn {order_id}. "
            f"Video: {args['video']}. Lý do hoàn: {args['reason']}."))]))
        self.history.append(types.Content(role="model", parts=[types.Part(text=text)]))
        return self._reply(text)

    # --- thực thi tool ---

    def _handle_call(self, name: str, args: dict) -> dict | None:
        """Trả về kết quả tool để gửi lại cho Gemini, hoặc None nếu cần dừng chờ duyệt."""
        if name not in self.tools:
            self.trace.step(f"LLM GỌI TOOL KHÔNG TỒN TẠI: {name}", [
                f"Tham số: {trace.dump(args)}",
                "→ không thực thi; trả {ok: false, error: unknown tool} cho LLM",
            ])
            self._event(name, args, policy.BLOCKED, "tool không tồn tại", "blocked")
            return {"ok": False, "error": f"unknown tool: {name}"}

        order, _ = tools.find_order(self.orders, args.get("order_id"))
        decision = policy.evaluate(name, args, order)
        next_action = {
            policy.AUTO: "→ chạy tool ngay, không cần duyệt",
            policy.NEEDS_APPROVAL: "→ tạm dừng lượt, chờ nhân viên duyệt",
            policy.BLOCKED: "→ không chạy tool; trả {status: blocked, reason} cho LLM giải thích với khách",
        }[decision.level]
        self.trace.step(f"POLICY KIỂM TRA TOOL {name}", [
            f"Tham số LLM đề xuất: {trace.dump(args)}",
            f"Dữ liệu policy dựa vào: {self._order_snapshot(args, order)}",
            f"Quyết định: {decision.level} — {decision.reason}",
            next_action,
        ])

        if decision.level == policy.BLOCKED:
            self._event(name, args, decision.level, decision.reason, "blocked")
            if policy.offers_return(name, decision, order):
                self._return_offer = order["order_id"]
                self.trace.step("MỜI KHÁCH LÀM YÊU CẦU HOÀN HÀNG", [
                    f"Huỷ đơn {order['order_id']} bị chặn vì đơn đã giao, đơn đủ điều kiện hoàn hàng",
                    "→ API trả offer_return cùng câu trả lời cuối, UI hiện dialog hỏi khách",
                ])
            return {"status": "blocked", "reason": decision.reason}
        if decision.level == policy.NEEDS_APPROVAL:
            self._pending = PendingApproval(name, args, decision.reason)
            self._pending_since = time.perf_counter()
            self._event(name, args, decision.level, decision.reason, "awaiting_approval")
            waiting = len(self._calls) - len(self._results) - 1
            self.trace.step("TẠM DỪNG CHỜ NHÂN VIÊN DUYỆT", [
                f"Yêu cầu: {name}({trace.dump(args)}) — {decision.reason}",
                "→ API trả status=pending_approval; lượt này chạy tiếp khi nhân viên gọi /approval",
                *([f"Còn {waiting} tool call khác của bước này chưa xử lý"] if waiting else []),
            ])
            return None
        return self._execute(name, args, decision.level, decision.reason)

    def _execute(self, name: str, args: dict, level: str, reason: str) -> dict:
        outcome = "executed" if level == policy.AUTO else "approved"
        started = time.perf_counter()
        try:
            result = self.tools[name](**args)
        except TypeError:
            result, outcome = {"ok": False, "error": "invalid or missing arguments"}, "error"
        except Exception as e:
            result, outcome = {"ok": False, "error": f"tool failed: {type(e).__name__}"}, "error"
        self._tool_runs += 1
        self._event(name, args, level, reason, outcome)
        self.trace.step(f"THỰC THI TOOL {name}" + (" (đã được nhân viên duyệt)" if level == policy.NEEDS_APPROVAL else ""), [
            f"Tham số: {trace.dump(args)}",
            f"Kết quả: {trace.dump(result)}",
            f"Outcome: {outcome} ({(time.perf_counter() - started) * 1000:.0f} ms)",
        ])
        return result

    def _collect(self, name: str, result: dict) -> None:
        self._results.append(types.Part.from_function_response(name=name, response=result))

    @staticmethod
    def _order_snapshot(args: dict, order: dict | None) -> str:
        if "order_id" not in args:
            return "không áp dụng (tool không gắn với một đơn)"
        if order is None:
            return "mã đơn không hợp lệ hoặc không tồn tại"
        return (f"đơn {order['order_id']}: trạng thái={order['status']}, thanh toán={order['payment_status']}, "
                f"tổng={order['total']}, đã hoàn={order['refunded_amount']}")

    # --- tiện ích ---

    def _event(self, tool: str, args: dict, level: str, reason: str, outcome: str) -> None:
        self._events.append(ToolEvent(tool, args, level, reason, outcome))

    def _rollback(self) -> None:
        self.history = self.history[:self._checkpoint]
        self._calls, self._results, self._pending = [], [], None
        self._return_offer = None

    def _reply(self, text: str, offer_return: str | None = None) -> TurnResult:
        total = time.perf_counter() - self._turn_started
        self.trace.step("TRẢ LỜI KHÁCH", [
            "Câu trả lời:",
            *[f"  {line}" for line in text.splitlines()],
            f"Tổng kết lượt: {total:.2f}s (trong đó chờ duyệt {self._wait_seconds:.1f}s) · "
            f"{self._step} lần gọi LLM · {self._tool_runs} lần thực thi tool",
        ])
        self.trace.end_turn()
        return TurnResult(REPLY, reply=text, tool_events=self._events, offer_return=offer_return)

    def _escalate(self, summary: str) -> TurnResult:
        summary = f"{summary} Tin nhắn khách: {self._message[:200]}"
        ticket = tools.escalate_to_human(summary)
        self.trace.step("ESCALATE (hệ thống tự chuyển cho nhân viên, không phải LLM)", [
            summary,
            f"Ticket: {ticket['ticket_id']}",
        ])
        return self._reply("Yêu cầu này cần nhân viên hỗ trợ xử lý, tôi đã chuyển cho họ.\n"
                           "Tóm tắt: agent không thể tự xử lý yêu cầu này\n"
                           "Đề xuất: chờ nhân viên liên hệ\n"
                           "Trạng thái: ĐÃ ESCALATE")
