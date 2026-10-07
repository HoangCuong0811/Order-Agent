"""CLI: `python main.py` để chat, `python main.py scan` để quét đơn hàng."""

import os
import sys

from dotenv import load_dotenv

MAX_MESSAGE_LENGTH = 1000


def validate_message(text: str) -> str | None:
    """Trả về thông báo lỗi nếu tin nhắn không hợp lệ, ngược lại None."""
    if not text.strip():
        return "Tin nhắn trống, vui lòng nhập nội dung."
    if len(text) > MAX_MESSAGE_LENGTH:
        return f"Tin nhắn quá dài ({len(text)} ký tự, tối đa {MAX_MESSAGE_LENGTH})."
    return None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # để in tiếng Việt trên console Windows
    sys.stdin.reconfigure(encoding="utf-8")
    load_dotenv()

    if not os.getenv("GEMINI_API_KEY"):
        print("Thiếu GEMINI_API_KEY. Hãy copy .env.example thành .env rồi điền API key.")
        return 1

    # Import sau khi load .env và kiểm tra key để lỗi hiển thị rõ ràng.
    import agent
    import tools
    import agent_trace as trace

    tools.load_orders()
    client = agent.make_client()
    scan_mode = len(sys.argv) > 1 and sys.argv[1] == "scan"
    trace.start_session("scan" if scan_mode else "chat", agent.model_name())

    if scan_mode:
        try:
            print(agent.run_scan(client))
        except Exception as e:
            print(f"Không thể chạy scan ({type(e).__name__}). Kiểm tra API key và kết nối mạng.")
            return 1
        return 0

    support = agent.SupportAgent(client)
    print("Tôi có thể giúp gì cho bạn? Gõ 'exit' để thoát.\n")
    while True:
        try:
            text = input("Khách: ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.strip().lower() in ("exit", "quit"):
            break
        error = validate_message(text)
        if error:
            print(f"[!] {error}\n")
            continue
        print(f"\nAgent: {support.chat(text)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
