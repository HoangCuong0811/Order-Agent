# Customer Support Agent

Agent chăm sóc khách hàng nhỏ chạy trên CLI (Python + Gemini API). Agent nhận câu hỏi về đơn hàng, tự xử lý những việc nằm trong quyền hạn, và chuyển cho con người duyệt hoặc escalate khi vượt quyền.

## Cài đặt

Yêu cầu: Python 3.10 trở lên và một Gemini API key.

**1. Tạo và kích hoạt môi trường ảo**

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

> Nếu máy có nhiều bản Python và `python` trỏ nhầm sang bản khác (ví dụ bản đi kèm LibreOffice), hãy dùng `py -3 -m venv .venv` trên Windows. Sau khi kích hoạt venv, `python` sẽ luôn là bản trong `.venv`.

**2. Cài thư viện**

```bash
pip install -r requirements.txt
```

**3. Tạo file `.env` và điền biến môi trường**

```powershell
# Windows (PowerShell)
Copy-Item .env.example .env
```

```bash
# macOS / Linux
cp .env.example .env
```

Mở `.env` và cập nhật:

| Biến | Ý nghĩa |
|---|---|
| `GEMINI_API_KEY` | API key của bạn (lấy tại Google AI Studio). **Bắt buộc.** |
| `GEMINI_MODEL` | Tên model Gemini. Mặc định `gemini-2.5-flash`. |

File `.env` đã nằm trong `.gitignore`, không commit key lên git.

## Cách chạy demo

```bash
python src/main.py          # chế độ chat với agent
python src/main.py scan     # quét toàn bộ đơn mẫu và báo cáo đơn có vấn đề
```

Chạy từ thư mục gốc của project (nơi có `.env`) sau khi đã kích hoạt venv.

Trong chế độ chat, gõ `exit` để thoát. Dữ liệu mẫu nằm ở `data/orders.json` (9 đơn, `ORD-1001` đến `ORD-1009`) và được nạp lại mỗi lần chạy.

### Kịch bản gợi ý

| # | Gõ vào | Kết quả mong đợi |
|---|---|---|
| 1 | `Đơn ORD-1001 của tôi đang ở đâu?` | Tra cứu và trả lời tự động |
| 2 | `Huỷ đơn ORD-1002` (đang `processing`) | Huỷ tự động |
| 3 | `Hoàn 200$ cho đơn ORD-1004` | CLI hiện khung "CẦN DUYỆT", bạn nhập `y` hoặc `n` |
| 4 | `Huỷ đơn ORD-1005` (đã `delivered`) | Bị chặn, agent giải thích lý do |
| 5 | `Hoàn 30$ cho ORD-1005` rồi `hoàn thêm 30$` | Lần 1 tự động, lần 2 cần duyệt (tổng vượt 50) |
| 6 | `Xoá tài khoản của tôi` | Agent escalate cho người |
| 7 | `Đơn của tôi đâu?` / `ORD-9999` / Enter trống | Hỏi lại mã đơn / báo không tồn tại / từ chối nhập trống |
| 8 | `python src/main.py scan` | Báo cáo đơn ORD-1003, 1006, 1007, 1008 kèm đề xuất |

Mỗi lần agent gọi tool, CLI in ra một dòng `[tool] ... -> AUTO | NEEDS_APPROVAL | BLOCKED` để bạn thấy quyết định của guardrail.

## Architecture

```
Khách nhập tin nhắn
      │
Validate input (rỗng / quá 1000 ký tự → từ chối, không gọi LLM)
      │
Gemini (function calling) ──► đề xuất gọi tool
      │
Policy layer (src/policy.py, thuần code)
      ├─ AUTO ──────────► chạy tool, trả kết quả cho Gemini
      ├─ NEEDS_APPROVAL ► CLI hỏi y/n
      │                     ├─ y: chạy tool
      │                     └─ n: không chạy, báo Gemini "rejected_by_human"
      └─ BLOCKED ───────► không chạy, trả lý do cho Gemini
      │
Gemini trả lời: Tóm tắt · Đề xuất · Trạng thái
```

```
customer-support-agent/
├── src/                 # toàn bộ code
│   ├── main.py
│   ├── agent.py
│   ├── policy.py
│   └── tools.py
├── data/
│   └── orders.json      # dữ liệu mẫu
├── requirements.txt
├── .env.example
├── DESIGN.md
└── README.md
```

| File | Vai trò |
|---|---|
| `src/main.py` | CLI: chế độ chat và `scan`, validate tin nhắn |
| `src/agent.py` | Vòng lặp gọi Gemini, gọi policy, hỏi duyệt, xử lý lỗi; hàm `run_scan` |
| `src/policy.py` | Luật guardrail (`evaluate`) và luật phát hiện vấn đề (`detect_issues`) |
| `src/tools.py` | Các tool: `get_order`, `cancel_order`, `issue_refund`, `escalate_to_human` |
| `data/orders.json` | Dữ liệu đơn hàng mẫu |

### Mức quyết định của guardrail

| Tool | Điều kiện | Mức |
|---|---|---|
| `get_order` | Chỉ đọc | AUTO |
| `cancel_order` | Đơn `processing` | AUTO |
| | Đơn `shipped` | NEEDS_APPROVAL |
| | Đơn `delivered` / `cancelled` | BLOCKED |
| `issue_refund` | Đơn đã thanh toán, tổng hoàn ≤ 50 | AUTO |
| | Đơn đã thanh toán, tổng hoàn > 50 | NEEDS_APPROVAL |
| | Chưa thanh toán / số tiền ≤ 0 / vượt số tiền còn hoàn được | BLOCKED |
| `escalate_to_human` | Luôn cho phép | AUTO |

### Luật phát hiện vấn đề (`detect_issues`)

`payment_failed` (thanh toán lỗi), `missing_address` (thiếu địa chỉ), `late_delivery` (quá hạn giao mà chưa giao), `high_value_pending` (đơn đang xử lý trên 500).

## Key design decisions

1. **Guardrail nằm trong code, không nằm trong prompt.** Prompt chỉ dặn Gemini cách cư xử, nhưng quyền chạy một hành động do `src/policy.py` quyết định dựa trên dữ liệu thật của đơn. Gemini không thể lách bằng cách diễn đạt khác, và khách nói gì cũng không đổi được kết quả.
2. **Tự điều khiển vòng lặp tool-calling.** Tắt automatic function calling của SDK để chèn policy và bước duyệt vào giữa "Gemini đề xuất" và "tool chạy".
3. **Mặc định chặn (fail closed).** Tool lạ, tham số sai hoặc không đọc được thì bị `BLOCKED`. Nhập sai hoặc không nhập ở bước duyệt (EOF) được tính là từ chối.
4. **Tính tổng tiền đã hoàn của đơn**, không tính từng lần riêng lẻ. Nhờ vậy không thể chia nhỏ khoản hoàn lớn thành nhiều khoản ≤ 50 để né bước duyệt.
5. **Human approval ngay trên CLI.** Cơ chế là một điểm dừng trong vòng lặp agent; có thể thay bằng API hoặc UI mà không đổi logic policy.
6. **`scan` chỉ đọc.** Luật phát hiện chạy bằng code (kết quả ổn định), Gemini chỉ viết tóm tắt và đề xuất. `scan` không thực thi hành động nào.
7. **Giới hạn 5 lượt gọi tool mỗi tin nhắn.** Vượt thì dừng và tự escalate để tránh vòng lặp vô hạn.
8. **Lỗi không làm crash CLI.** Lỗi API được bắt, in thông báo thân thiện, và lịch sử hội thoại được khôi phục về trước lượt lỗi.
9. **Ngày "hôm nay" cố định** (`policy.TODAY = 2026-10-07`) để dữ liệu mẫu cho kết quả `late_delivery` giống nhau mỗi lần demo.

## Known limitations

- **Không xác thực khách hàng.** Ai biết mã đơn đều xem và thao tác được đơn đó. Bản thật cần đăng nhập hoặc xác minh email trước khi trả dữ liệu đơn.
- **Dữ liệu chỉ lưu trong bộ nhớ.** Huỷ đơn và hoàn tiền mất khi thoát chương trình; không có database, không ghi audit log ra file.
- **Một khách, một phiên.** Không có nhiều người dùng đồng thời, lịch sử hội thoại không được lưu giữa các lần chạy, và hội thoại dài sẽ không bị cắt bớt.
- **Hoàn tiền và escalate chỉ giả lập.** Không gọi cổng thanh toán hay hệ thống ticket thật; `escalate_to_human` chỉ in ra màn hình.
- **Huỷ đơn không tự hoàn tiền.** Hoàn tiền là một thao tác riêng, đi qua guardrail riêng.
- **Phụ thuộc vào Gemini.** Cần mạng và API key hợp lệ; chất lượng câu trả lời và việc tuân thủ định dạng 3 dòng cuối phụ thuộc model. Guardrail vẫn an toàn dù model trả lời sai định dạng.
- **Ngưỡng và luật là hằng số** trong `src/policy.py` (ngưỡng hoàn tự động 50, đơn giá trị cao 500), chưa cấu hình được từ ngoài.
- **Chưa có test tự động** và chưa kiểm thử với nhiều model Gemini khác nhau.
- **Chưa có cơ chế chống prompt injection riêng.** Guardrail vẫn chặn được hành động nhạy cảm, nhưng agent có thể bị dẫn dắt trả lời lạc đề.
