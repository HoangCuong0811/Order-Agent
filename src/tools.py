"""Các tool mà agent được phép gọi. Việc cho phép chạy hay không do policy.py quyết định."""

import json
import re
from pathlib import Path

import policy

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "orders.json"
ORDER_ID_PATTERN = re.compile(r"^ORD-\d{4}$")

ORDERS: dict[str, dict] = {}  # dữ liệu chỉ nằm trong bộ nhớ, reset mỗi lần chạy
_ticket_count = 0


def load_orders() -> None:
    ORDERS.clear()
    for order in json.loads(DATA_FILE.read_text(encoding="utf-8")):
        order["refunded_amount"] = 0.0
        ORDERS[order["order_id"]] = order


def find_order(order_id) -> tuple[dict | None, str | None]:
    """Trả về (order, None) nếu tìm thấy, ngược lại (None, mã lỗi)."""
    oid = str(order_id or "").strip().upper()
    if not ORDER_ID_PATTERN.match(oid):
        return None, "invalid_order_id: expected format ORD-1234"
    order = ORDERS.get(oid)
    if order is None:
        return None, f"not_found: no order {oid}"
    return order, None


def get_order(order_id: str) -> dict:
    """Look up an order by its ID (format ORD-1234). Returns order details and any detected issues."""
    order, error = find_order(order_id)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "order": order, "issues": policy.detect_issues(order)}


def cancel_order(order_id: str) -> dict:
    """Cancel an order. Only call when the customer clearly asks to cancel."""
    order, error = find_order(order_id)
    if error:
        return {"ok": False, "error": error}
    order["status"] = "cancelled"
    return {"ok": True, "order_id": order["order_id"], "new_status": "cancelled"}


def issue_refund(order_id: str, amount: float, reason: str) -> dict:
    """Refund part or all of an order's payment. amount is in the order's currency, reason is a short text."""
    order, error = find_order(order_id)
    if error:
        return {"ok": False, "error": error}
    amount = round(float(amount), 2)  # policy đã kiểm tra amount hợp lệ trước khi tool chạy
    order["refunded_amount"] = round(order["refunded_amount"] + amount, 2)
    if order["refunded_amount"] >= order["total"]:
        order["payment_status"] = "refunded"
    return {"ok": True, "order_id": order["order_id"], "refunded": amount, "reason": reason,
            "total_refunded": order["refunded_amount"]}


def escalate_to_human(summary: str) -> dict:
    """Hand the case to a human agent. Use for anything outside your tools or authority
    (account deletion, complaints, compensation, legal issues) or when you are unsure."""
    global _ticket_count
    _ticket_count += 1
    ticket_id = f"ESC-{_ticket_count:03d}"
    print(f"  [ESCALATE] {ticket_id}: {summary}")
    return {"ok": True, "ticket_id": ticket_id}

tools = [get_order, cancel_order, issue_refund, escalate_to_human]
TOOLS = {f.__name__: f for f in tools}
