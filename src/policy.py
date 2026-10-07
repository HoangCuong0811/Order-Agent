"""Guardrails và luật phát hiện vấn đề. Thuần code, không dùng LLM.

Quy tắc cốt lõi: thao tác động tới tiền (huỷ đơn, hoàn tiền) KHÔNG có đường tự động. Chúng luôn cần
nhân viên duyệt, bất kể trạng thái đơn hay số tiền. BLOCKED chỉ dành cho yêu cầu không thể thực hiện.
"""

import math
from dataclasses import dataclass
from datetime import date

AUTO = "AUTO"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
BLOCKED = "BLOCKED"

HIGH_VALUE_THRESHOLD = 500.0  # đơn đang xử lý có giá trị trên mức này cần kiểm tra thủ công

# Ngày cố định để dữ liệu mẫu cho kết quả giống nhau mỗi lần chạy demo.
TODAY = date(2026, 10, 7)

SAFE_TOOLS = {"get_order", "escalate_to_human"}


@dataclass
class Decision:
    level: str
    reason: str


def evaluate(tool_name: str, args: dict, order: dict | None) -> Decision:
    """Quyết định một lần gọi tool: chạy tự động (chỉ tra cứu / escalate), cần duyệt, hay bị chặn."""
    if tool_name == "cancel_order":
        return _check_cancel(order)
    if tool_name == "issue_refund":
        return _check_refund(args, order)
    if tool_name in SAFE_TOOLS:
        return Decision(AUTO, "chỉ đọc hoặc chuyển cho người")
    return Decision(BLOCKED, f"tool không được phép: {tool_name}")  # mặc định: chặn


def _check_cancel(order: dict | None) -> Decision:
    if order is None:
        return Decision(BLOCKED, "mã đơn không hợp lệ hoặc không tồn tại")
    status = order["status"]
    if status == "processing":
        return Decision(AUTO, "Đơn hàng này chỉ đang chuẩn bị, có thể tự động hủy")
    if status == "shipped":
        return Decision(NEEDS_APPROVAL, "huỷ đơn luôn cần nhân viên duyệt (đơn đã gửi đi)")
    if status == "delivered" and evaluate_return(order).level == NEEDS_APPROVAL:
        return Decision(BLOCKED, "không thể huỷ đơn đã giao; khách có thể làm yêu cầu hoàn hàng thay thế "
                                 "(hệ thống sẽ hiện form để khách nhập)")
    return Decision(BLOCKED, f"không thể huỷ đơn ở trạng thái '{status}'")


def refundable_amount(order: dict) -> float:
    """Số tiền còn hoàn được của đơn (tổng trừ phần đã hoàn)."""
    return round(order["total"] - order["refunded_amount"], 2)


def evaluate_return(order: dict | None) -> Decision:
    """Quyết định yêu cầu hoàn hàng của khách. Không phải tool của LLM nên không đi qua `evaluate`."""
    if order is None:
        return Decision(BLOCKED, "mã đơn không hợp lệ hoặc không tồn tại")
    if order["status"] != "delivered":
        return Decision(BLOCKED, f"chỉ đơn đã giao mới hoàn hàng được, đơn này đang ở trạng thái '{order['status']}'")
    if order["payment_status"] != "paid":
        return Decision(BLOCKED, f"đơn có trạng thái thanh toán '{order['payment_status']}', không thể hoàn tiền")
    if refundable_amount(order) <= 0:
        return Decision(BLOCKED, "đơn đã được hoàn hết tiền")
    return Decision(NEEDS_APPROVAL, "hoàn hàng (kéo theo hoàn tiền) luôn cần nhân viên duyệt")


def offers_return(tool_name: str, decision: Decision, order: dict | None) -> bool:
    """Huỷ đơn bị chặn vì đơn đã giao, và đơn đủ điều kiện hoàn hàng: UI nên mời khách làm yêu cầu hoàn hàng."""
    return (tool_name == "cancel_order" and decision.level == BLOCKED and order is not None
            and order["status"] == "delivered" and evaluate_return(order).level == NEEDS_APPROVAL)


def _check_refund(args: dict, order: dict | None) -> Decision:
    if order is None:
        return Decision(BLOCKED, "mã đơn không hợp lệ hoặc không tồn tại")
    try:
        amount = float(args.get("amount"))
    except (TypeError, ValueError):
        return Decision(BLOCKED, "số tiền hoàn không hợp lệ")
    if not math.isfinite(amount) or amount <= 0:
        return Decision(BLOCKED, "số tiền hoàn phải lớn hơn 0")
    if order["payment_status"] != "paid":
        return Decision(BLOCKED, f"đơn có trạng thái thanh toán '{order['payment_status']}', không thể hoàn tiền")

    already = order["refunded_amount"]
    if amount + already > order["total"] + 1e-9:
        remaining = order["total"] - already
        return Decision(BLOCKED, f"vượt quá số tiền còn có thể hoàn ({remaining:.2f})")
    return Decision(NEEDS_APPROVAL, "hoàn tiền luôn cần nhân viên duyệt, bất kể số tiền")


def detect_issues(order: dict) -> list[str]:
    """Trả về danh sách vấn đề của một đơn (rỗng nếu không có)."""
    issues = []
    status = order["status"]
    if status in ("cancelled", "returned"):
        return issues

    if order["payment_status"] == "failed":
        issues.append("payment_failed: thanh toán thất bại")
    if not order["shipping_address"].strip():
        issues.append("missing_address: thiếu địa chỉ giao hàng")
    if status in ("processing", "shipped"):
        days_late = (TODAY - date.fromisoformat(order["expected_delivery"])).days
        if days_late > 0:
            issues.append(f"late_delivery: giao trễ {days_late} ngày")
    if order["total"] > HIGH_VALUE_THRESHOLD:
        issues.append(f"high_value_pending: đơn giá trị cao ({order['total']:.2f}) đang chờ xử lý")
    return issues
