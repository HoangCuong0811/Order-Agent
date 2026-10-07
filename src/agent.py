"""Vòng lặp agent: Gemini đề xuất gọi tool, policy quyết định, người duyệt khi cần."""

import json
import os
import time

from google import genai
from google.genai import types

import policy
import tools
import agent_trace as trace

MAX_TOOL_CALLS = 5  # giới hạn số lượt gọi tool cho mỗi tin nhắn của khách

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

SCAN_PROMPT = """You are an operations assistant for an online shop. Below are orders flagged
by automatic rules. For each one write a short block in Vietnamese:
[ORDER_ID] <issue>
  Tóm tắt: <1 sentence>
  Đề xuất: <recommended next action for the support staff>
Only recommend; do not claim any action was taken. Use only the data given."""


def make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def model_name() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")


def ask_human(tool_name: str, args: dict, reason: str) -> bool:
    """Hỏi người duyệt trên CLI. Mặc định từ chối nếu không nhận được câu trả lời."""
    print("\n  +-- CẦN DUYỆT -------------------------------------")
    print(f"  | Hành động : {tool_name}")
    print(f"  | Tham số   : {json.dumps(args, ensure_ascii=False)}")
    print(f"  | Lý do     : {reason}")
    print("  +--------------------------------------------------")
    while True:
        try:
            answer = input("  Duyệt? [y/n]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


class SupportAgent:
    def __init__(self, client: genai.Client):
        self.client = client
        self.history: list[types.Content] = []
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=list(tools.TOOLS.values()),
            # Tắt tự động gọi tool để mình chèn policy vào giữa.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def chat(self, message: str) -> str:
        checkpoint = len(self.history)
        try:
            return self._run_turn(message)
        except Exception as e:
            self.history = self.history[:checkpoint]  # bỏ lượt dở để lịch sử không bị hỏng
            trace.log(f"LỖI {type(e).__name__}", str(e))
            return f"Xin lỗi, hệ thống đang gặp lỗi ({type(e).__name__}). Vui lòng thử lại sau."

    def _run_turn(self, message: str) -> str:
        checkpoint = len(self.history)
        self.history.append(types.Content(role="user", parts=[types.Part(text=message)]))
        trace.log("-" * 60)
        trace.log("KHÁCH", message)

        for step in range(1, MAX_TOOL_CALLS + 1):
            started = time.perf_counter()
            response = self.client.models.generate_content(
                model=model_name(), contents=self.history, config=self.config
            )
            trace.log_llm_call(step, model_name(), time.perf_counter() - started, response)
            if not response.candidates or response.candidates[0].content is None:
                self.history = self.history[:checkpoint]
                return self._escalate(f"Gemini không trả về nội dung. Tin nhắn khách: {message[:200]}")
            self.history.append(response.candidates[0].content)

            calls = response.function_calls
            if not calls:
                answer = response.text or "Xin lỗi, tôi chưa có câu trả lời phù hợp."
                trace.log("TRẢ LỜI KHÁCH", answer)
                return answer

            parts = [
                types.Part.from_function_response(name=c.name, response=self._run_tool(c.name, dict(c.args or {})))
                for c in calls
            ]
            self.history.append(types.Content(role="user", parts=parts))

        # Quá số lượt gọi tool cho phép: dừng và giao cho người.
        self.history = self.history[:checkpoint]
        return self._escalate(f"Agent vượt quá {MAX_TOOL_CALLS} lượt gọi tool. Tin nhắn khách: {message[:200]}")

    def _escalate(self, summary: str) -> str:
        trace.log("ESCALATE (do hệ thống, không phải LLM)", summary)
        tools.escalate_to_human(summary)
        return ("Yêu cầu này cần nhân viên hỗ trợ xử lý, tôi đã chuyển cho họ.\n"
                "Tóm tắt: agent không thể tự xử lý yêu cầu này\n"
                "Đề xuất: chờ nhân viên liên hệ\n"
                "Trạng thái: ĐÃ ESCALATE")

    def _run_tool(self, name: str, args: dict) -> dict:
        result = self._execute_tool(name, args)
        trace.log(f"  Kết quả tool gửi lại cho LLM: {name}", result)
        return result

    def _execute_tool(self, name: str, args: dict) -> dict:
        fn = tools.TOOLS.get(name)
        if fn is None:
            trace.log(f"  TOOL {name}: không tồn tại", args)
            return {"ok": False, "error": f"unknown tool: {name}"}

        order, _ = tools.find_order(args.get("order_id"))
        decision = policy.evaluate(name, args, order)
        print(f"  [tool] {name}({json.dumps(args, ensure_ascii=False)}) -> {decision.level}")
        trace.log(f"  TOOL {name}({json.dumps(args, ensure_ascii=False)})")
        trace.log(f"  Policy: {decision.level}", decision.reason)

        if decision.level == policy.BLOCKED:
            return {"status": "blocked", "reason": decision.reason}
        if decision.level == policy.NEEDS_APPROVAL:
            approved = ask_human(name, args, decision.reason)
            trace.log(f"  Người duyệt: {'ĐỒNG Ý' if approved else 'TỪ CHỐI'}")
            if not approved:
                return {"status": "rejected_by_human", "reason": "nhân viên không duyệt yêu cầu này"}

        try:
            result = fn(**args)
        except TypeError:
            return {"ok": False, "error": "invalid or missing arguments"}
        except Exception as e:
            return {"ok": False, "error": f"tool failed: {type(e).__name__}"}
        if decision.level == policy.NEEDS_APPROVAL:
            result["approved_by_human"] = True
        return result


def run_scan(client: genai.Client) -> str:
    """Quét mọi đơn bằng luật cố định, nhờ Gemini viết tóm tắt và đề xuất. Chỉ đọc, không thực thi gì."""
    flagged = []
    for order in tools.ORDERS.values():
        issues = policy.detect_issues(order)
        if issues:
            flagged.append({"order_id": order["order_id"], "customer": order["customer_name"],
                            "total": order["total"], "status": order["status"],
                            "payment_status": order["payment_status"], "issues": issues})

    header = f"Đã quét {len(tools.ORDERS)} đơn, phát hiện {len(flagged)} đơn cần chú ý."
    trace.log("SCAN: kết quả detect_issues (luật cố định, chưa dùng LLM)", flagged)
    if not flagged:
        return header

    started = time.perf_counter()
    response = client.models.generate_content(
        model=model_name(),
        contents=json.dumps(flagged, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=SCAN_PROMPT,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    trace.log_llm_call(1, model_name(), time.perf_counter() - started, response)
    trace.log("SCAN: báo cáo Gemini viết", response.text)
    return f"{header}\n\n{response.text or '(Gemini không trả về nội dung)'}"
