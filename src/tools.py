"""Các tool mà agent được phép gọi. Việc cho phép chạy hay không do policy.py quyết định.

Dữ liệu đơn là riêng của từng phiên: mỗi phiên nhận một bản sao của dữ liệu mẫu (`new_session_orders`)
và tool được tạo gắn với bản sao đó (`make_tools`), nên thay đổi trong một phiên không ảnh hưởng phiên khác.
"""

import copy
import json
import re
from pathlib import Path
from typing import Callable

import policy

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "orders.json"
ORDER_ID_PATTERN = re.compile(r"^ORD-\d{4}$")

TEMPLATE: list[dict] = []  # dữ liệu mẫu đọc từ file, không bao giờ bị tool sửa
_ticket_count = 0


def load_orders() -> None:
    """Đọc dữ liệu mẫu từ file (gọi một lần lúc khởi động server)."""
    TEMPLATE.clear()
    TEMPLATE.extend(json.loads(DATA_FILE.read_text(encoding="utf-8")))


def new_session_orders() -> dict[str, dict]:
    """Bản sao dữ liệu mẫu cho một phiên mới. Sửa bản sao này không đụng tới dữ liệu mẫu hay phiên khác."""
    orders = {}
    for order in TEMPLATE:
        copied = copy.deepcopy(order)
        copied["refunded_amount"] = 0.0
        orders[copied["order_id"]] = copied
    return orders


def find_order(orders: dict[str, dict], order_id) -> tuple[dict | None, str | None]:
    """Trả về (order, None) nếu tìm thấy trong `orders` của phiên, ngược lại (None, mã lỗi)."""
    oid = str(order_id or "").strip().upper()
    if not ORDER_ID_PATTERN.match(oid):
        return None, "invalid_order_id: expected format ORD-1234"
    order = orders.get(oid)
    if order is None:
        return None, f"not_found: no order {oid}"
    return order, None


def escalate_to_human(summary: str) -> dict:
    """Hand the case to a human agent. Use for anything outside your tools or authority
    (account deletion, complaints, compensation, legal issues) or when you are unsure."""
    global _ticket_count
    _ticket_count += 1
    ticket_id = f"ESC-{_ticket_count:03d}"
    print(f"  [ESCALATE] {ticket_id}: {summary}")
    return {"ok": True, "ticket_id": ticket_id}


def return_order(orders: dict[str, dict], order_id: str, video: str, reason: str) -> dict:
    """Thực hiện hoàn hàng đã được nhân viên duyệt: đơn thành `returned` và hoàn toàn bộ số tiền còn lại.
    Không nằm trong `make_tools`: LLM không được thấy hay gọi tool này,
    chỉ agent gọi sau khi khách gửi yêu cầu và nhân viên duyệt."""
    order, error = find_order(orders, order_id)
    if error:
        return {"ok": False, "error": error}
    amount = policy.refundable_amount(order)  # policy đã kiểm tra đơn đủ điều kiện hoàn hàng
    order["status"] = "returned"
    order["refunded_amount"] = round(order["refunded_amount"] + amount, 2)
    order["payment_status"] = "refunded"
    order["return_request"] = {"video": video, "reason": reason}
    return {"ok": True, "order_id": order["order_id"], "new_status": "returned", "refunded": amount,
            "total_refunded": order["refunded_amount"]}


def make_tools(orders: dict[str, dict]) -> dict[str, Callable]:
    """Các tool khai báo cho LLM, gắn với dữ liệu đơn của một phiên.
    Tên hàm, chữ ký và docstring được giữ nguyên vì Gemini dựng khai báo tool từ chúng."""

    def get_order(order_id: str) -> dict:
        """Look up an order by its ID (format ORD-1234). Returns order details and any detected issues."""
        order, error = find_order(orders, order_id)
        if error:
            return {"ok": False, "error": error}
        return {"ok": True, "order": order, "issues": policy.detect_issues(order)}

    def cancel_order(order_id: str) -> dict:
        """Cancel an order. Only call when the customer clearly asks to cancel."""
        order, error = find_order(orders, order_id)
        if error:
            return {"ok": False, "error": error}
        order["status"] = "cancelled"
        return {"ok": True, "order_id": order["order_id"], "new_status": "cancelled"}

    def issue_refund(order_id: str, amount: float, reason: str) -> dict:
        """Refund part or all of an order's payment. amount is in the order's currency, reason is a short text."""
        order, error = find_order(orders, order_id)
        if error:
            return {"ok": False, "error": error}
        amount = round(float(amount), 2)  # policy đã kiểm tra amount hợp lệ trước khi tool chạy
        order["refunded_amount"] = round(order["refunded_amount"] + amount, 2)
        if order["refunded_amount"] >= order["total"]:
            order["payment_status"] = "refunded"
        return {"ok": True, "order_id": order["order_id"], "refunded": amount, "reason": reason,
                "total_refunded": order["refunded_amount"]}

    return {f.__name__: f for f in [get_order, cancel_order, issue_refund, escalate_to_human]}
