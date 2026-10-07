"""Guardrails và luật phát hiện vấn đề. Thuần code, không dùng LLM."""

import math
from dataclasses import dataclass
from datetime import date

AUTO = "AUTO"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
BLOCKED = "BLOCKED"

REFUND_AUTO_LIMIT = 50.0  # tổng tiền hoàn tự động tối đa cho một đơn
HIGH_VALUE_THRESHOLD = 500.0  # đơn đang xử lý có giá trị trên mức này cần kiểm tra thủ công

# Ngày cố định để dữ liệu mẫu cho kết quả giống nhau mỗi lần chạy demo.
TODAY = date(2026, 10, 7)

SAFE_TOOLS = {"get_order", "escalate_to_human"}


@dataclass
class Decision:
    level: str
    reason: str


def evaluate(tool_name: str, args: dict, order: dict | None) -> Decision:
    """Quyết định một lần gọi tool được chạy tự động, cần duyệt hay bị chặn."""
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
        return Decision(AUTO, "đơn chưa gửi đi")
    if status == "shipped":
        return Decision(NEEDS_APPROVAL, "đơn đã gửi đi, huỷ cần nhân viên duyệt")
    return Decision(BLOCKED, f"không thể huỷ đơn ở trạng thái '{status}'")


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
    # Tính cả số đã hoàn trước đó để không lách luật bằng cách chia nhỏ nhiều lần.
    if amount + already <= REFUND_AUTO_LIMIT:
        return Decision(AUTO, f"tổng hoàn tiền không quá {REFUND_AUTO_LIMIT:.0f}")
    return Decision(NEEDS_APPROVAL, f"tổng hoàn tiền vượt {REFUND_AUTO_LIMIT:.0f}, cần nhân viên duyệt")


def detect_issues(order: dict) -> list[str]:
    """Trả về danh sách vấn đề của một đơn (rỗng nếu không có)."""
    issues = []
    status = order["status"]
    if status == "cancelled":
        return issues

    if order["payment_status"] == "failed":
        issues.append("payment_failed: thanh toán thất bại")
    if not order["shipping_address"].strip():
        issues.append("missing_address: thiếu địa chỉ giao hàng")
    if status in ("processing", "shipped"):
        days_late = (TODAY - date.fromisoformat(order["expected_delivery"])).days
        if days_late > 0:
            issues.append(f"late_delivery: giao trễ {days_late} ngày")
    if status == "processing" and order["total"] > HIGH_VALUE_THRESHOLD:
        issues.append(f"high_value_pending: đơn giá trị cao ({order['total']:.2f}) đang chờ xử lý")
    return issues
