"""Khởi động API server: `python src/main.py`. Giao diện chat nằm ở http://127.0.0.1:8000."""

import os
import sys

from dotenv import load_dotenv


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # để in tiếng Việt trên console Windows
    load_dotenv()

    if not os.getenv("GEMINI_API_KEY"):
        print("Thiếu GEMINI_API_KEY. Hãy copy .env.example thành .env rồi điền API key.")
        return 1

    # Import sau khi load .env và kiểm tra key để lỗi hiển thị rõ ràng.
    import uvicorn

    import api

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(api.app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
