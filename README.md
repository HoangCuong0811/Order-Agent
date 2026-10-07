# Order Support Chatbot

Chatbot hỗ trợ đơn hàng mô phỏng một sàn thương mại điện tử (Python + FastAPI + Gemini API). Agent nhận câu hỏi về đơn hàng, tự xử lý những việc nằm trong quyền hạn, và chuyển cho nhân viên duyệt hoặc escalate khi vượt quyền. Có REST API và một giao diện chat tối giản (nền đen) chạy trên trình duyệt.

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
| `GEMINI_MODEL` | Tên model Gemini. Mặc định `gemini-3.5-flash-lite`. |
| `HOST`, `PORT` | Địa chỉ và cổng của server. Mặc định `127.0.0.1:8000`. Không bắt buộc. |

File `.env` đã nằm trong `.gitignore`, không commit key lên git.

## Cách chạy

```bash
python src/main.py
```

Chạy từ thư mục gốc của project (nơi có `.env`) sau khi đã kích hoạt venv, rồi mở http://127.0.0.1:8000.

- Giao diện chat: `http://127.0.0.1:8000/`
- Tài liệu API tương tác (Swagger): `http://127.0.0.1:8000/docs`

Trong giao diện, bạn vừa là khách hàng vừa đóng vai **nhân viên CSKH**: khi một yêu cầu cần duyệt, thẻ "Cần nhân viên duyệt" hiện ra với hai nút Duyệt / Từ chối. Nút "Mới" ở góc phải bắt đầu cuộc trò chuyện mới. Để nạp lại dữ liệu đơn mẫu (thử lại huỷ đơn / hoàn tiền từ đầu), gọi `POST /api/demo/reset-orders` (ví dụ qua `/docs`) hoặc khởi động lại server.

### Kịch bản gợi ý

| # | Gõ vào | Kết quả mong đợi |
|---|---|---|
| 1 | `Đơn ORD-1001 của tôi đang ở đâu?` | Tra cứu và trả lời tự động |
| 2 | `Huỷ đơn ORD-1002` (đang `processing`) | Huỷ tự động |
| 3 | `Hoàn 200$ cho đơn ORD-1004` | Hiện thẻ "Cần nhân viên duyệt", bạn bấm Duyệt hoặc Từ chối |
| 4 | `Huỷ đơn ORD-1005` (đã `delivered`) | Bị chặn, agent giải thích lý do |
| 5 | `Hoàn 30$ cho ORD-1005` rồi `hoàn thêm 30$` | Lần 1 tự động, lần 2 cần duyệt (tổng vượt 50) |
| 6 | `Xoá tài khoản của tôi` | Agent escalate cho người |
| 7 | `Đơn của tôi đâu?` / `ORD-9999` | Hỏi lại mã đơn / báo không tồn tại |

Giao diện chỉ hiển thị câu trả lời và thẻ duyệt. Các tool agent đã gọi cùng quyết định của guardrail (`AUTO` / `NEEDS_APPROVAL` / `BLOCKED`) nằm trong trường `tool_events` của API, và trong console / `logs/agent_trace.log`.

## API

Mỗi cuộc trò chuyện là một **phiên** (session) có lịch sử riêng, lưu trong bộ nhớ server.

| Method | Đường dẫn | Mô tả |
|---|---|---|
| `POST` | `/api/sessions` | Tạo phiên mới, trả `{"session_id": "..."}` |
| `POST` | `/api/sessions/{id}/messages` | Gửi tin nhắn của khách, body `{"message": "..."}` |
| `POST` | `/api/sessions/{id}/approval` | Nhân viên duyệt hoặc từ chối, body `{"approved": true}` |
| `POST` | `/api/demo/reset-orders` | Nạp lại dữ liệu đơn mẫu |
| `GET` | `/api/health` | Kiểm tra server và model đang dùng |

`messages` và `approval` đều trả cùng một dạng kết quả:

```json
{
  "status": "reply",
  "reply": "toàn văn câu trả lời của agent",
  "body": "câu trả lời bỏ 3 dòng cuối",
  "summary": "...", "suggestion": "...", "case_status": "ĐÃ XỬ LÝ",
  "approval": null,
  "tool_events": [
    {"tool": "cancel_order", "args": {"order_id": "ORD-1002"}, "level": "AUTO",
     "reason": "đơn chưa gửi đi", "outcome": "executed"}
  ]
}
```

`status` là `reply` (agent đã trả lời xong) hoặc `pending_approval` (lượt hội thoại đang tạm dừng chờ duyệt, khi đó `approval` chứa hành động cần duyệt và `reply` là `null`). Gọi `approval` để chạy tiếp; trong lúc chờ duyệt, gửi thêm tin nhắn vào phiên đó sẽ nhận `409`.

Mã lỗi: `400` tin nhắn rỗng hoặc dài quá 1000 ký tự, `404` phiên không tồn tại (kể cả sau khi server khởi động lại), `409` sai thứ tự thao tác hoặc phiên đang bận.

```bash
curl -X POST http://127.0.0.1:8000/api/sessions
curl -X POST http://127.0.0.1:8000/api/sessions/<session_id>/messages \
     -H "Content-Type: application/json" -d '{"message": "Huỷ đơn ORD-1002"}'
```

## Architecture

```
Trình duyệt (src/static/index.html)  ──HTTP──►  FastAPI (src/api.py)
                                                      │  session → SupportAgent
                                                      ▼
Validate input (rỗng / quá 1000 ký tự → 400, không gọi LLM)
      │
Gemini (function calling) ──► đề xuất gọi tool
      │
Policy layer (src/policy.py, thuần code)
      ├─ AUTO ──────────► chạy tool, trả kết quả cho Gemini
      ├─ NEEDS_APPROVAL ► tạm dừng lượt, API trả status "pending_approval"
      │                     │  (nhân viên gọi /approval)
      │                     ├─ duyệt: kiểm tra lại policy, chạy tool, chạy tiếp
      │                     └─ từ chối: không chạy, báo Gemini "rejected_by_human"
      └─ BLOCKED ───────► không chạy, trả lý do cho Gemini
      │
Gemini trả lời: Tóm tắt · Đề xuất · Trạng thái
```

```
├── src/
│   ├── main.py          # khởi động server (uvicorn)
│   ├── api.py           # REST API, quản lý phiên, phục vụ UI
│   ├── agent.py         # vòng lặp Gemini + policy + tạm dừng/tiếp tục khi chờ duyệt
│   ├── policy.py        # guardrail (evaluate) và luật phát hiện vấn đề (detect_issues)
│   ├── tools.py         # get_order, cancel_order, issue_refund, escalate_to_human
│   ├── agent_trace.py   # ghi vết từng bước ra logs/agent_trace.log
│   └── static/index.html  # giao diện chat
├── data/orders.json     # dữ liệu đơn hàng mẫu (9 đơn)
├── requirements.txt
├── .env.example
├── DESIGN.md
└── README.md
```

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

`get_order` trả kèm danh sách vấn đề của đơn để agent tham khảo: `payment_failed` (thanh toán lỗi), `missing_address` (thiếu địa chỉ), `late_delivery` (quá hạn giao mà chưa giao), `high_value_pending` (đơn đang xử lý trên 500).

## Key design decisions

1. **Guardrail nằm trong code, không nằm trong prompt.** Prompt chỉ dặn Gemini cách cư xử, nhưng quyền chạy một hành động do `src/policy.py` quyết định dựa trên dữ liệu thật của đơn. Gemini không thể lách bằng cách diễn đạt khác, và khách nói gì cũng không đổi được kết quả.
2. **Tự điều khiển vòng lặp tool-calling.** Tắt automatic function calling của SDK để chèn policy và bước duyệt vào giữa "Gemini đề xuất" và "tool chạy".
3. **Duyệt bất đồng bộ qua API.** Agent không chặn thread để chờ người. Khi cần duyệt, trạng thái lượt (các tool call đang xử lý, kết quả đã có) được giữ trong phiên và `resolve_approval` chạy tiếp đúng từ chỗ dừng. Khi duyệt, policy được **đánh giá lại** vì dữ liệu có thể đã đổi trong lúc chờ (ví dụ phiên khác đã huỷ cùng đơn).
4. **Mặc định chặn (fail closed).** Tool lạ, tham số sai hoặc không đọc được thì bị `BLOCKED`. Không có quyết định duyệt thì tool không chạy.
5. **Tính tổng tiền đã hoàn của đơn**, không tính từng lần riêng lẻ. Nhờ vậy không thể chia nhỏ khoản hoàn lớn thành nhiều khoản ≤ 50 để né bước duyệt.
6. **Mỗi phiên xử lý một lượt tại một thời điểm.** Gửi tin khi phiên đang bận hoặc đang chờ duyệt trả `409`, để lịch sử hội thoại không bị ghi chồng.
7. **Giới hạn 5 lượt gọi Gemini mỗi tin nhắn.** Vượt thì dừng và tự escalate để tránh vòng lặp vô hạn.
8. **Lỗi không làm hỏng phiên.** Lỗi API được bắt, trả thông báo thân thiện, và lịch sử hội thoại được khôi phục về trước lượt lỗi.
9. **Ngày "hôm nay" cố định** (`policy.TODAY = 2026-10-07`) để dữ liệu mẫu cho kết quả `late_delivery` giống nhau mỗi lần demo.

## Known limitations

- **Không xác thực khách hàng.** Ai biết mã đơn đều xem và thao tác được đơn đó, và ai cũng gọi được `/approval`, `/api/demo/reset-orders`. Bản thật cần đăng nhập khách hàng, và endpoint duyệt phải dành riêng cho nhân viên đã xác thực.
- **Dữ liệu và phiên chỉ lưu trong bộ nhớ.** Huỷ đơn, hoàn tiền, lịch sử hội thoại và yêu cầu đang chờ duyệt mất khi server khởi động lại. Dữ liệu đơn dùng chung cho mọi phiên. Server giữ tối đa 200 phiên gần nhất, chạy một process.
- **Hoàn tiền và escalate chỉ giả lập.** Không gọi cổng thanh toán hay hệ thống ticket thật; `escalate_to_human` chỉ in ra console.
- **Huỷ đơn không tự hoàn tiền.** Hoàn tiền là một thao tác riêng, đi qua guardrail riêng.
- **Lượt lỗi không hoàn tác tool đã chạy.** Nếu một tool đã thực thi rồi Gemini mới lỗi, lịch sử được khôi phục nhưng thay đổi dữ liệu vẫn còn. Tổng tiền đã hoàn được tính vào guardrail nên không vượt hạn mức, nhưng khách có thể phải hỏi lại.
- **Phụ thuộc vào Gemini.** Cần mạng và API key hợp lệ; chất lượng câu trả lời và việc tuân thủ định dạng 3 dòng cuối phụ thuộc model. Guardrail vẫn an toàn dù model trả lời sai định dạng.
- **Ngưỡng và luật là hằng số** trong `src/policy.py` (ngưỡng hoàn tự động 50, đơn giá trị cao 500), chưa cấu hình được từ ngoài.
- **Chưa có test tự động trong repo** và chưa kiểm thử với nhiều model Gemini khác nhau.
- **Chưa có cơ chế chống prompt injection riêng.** Guardrail vẫn chặn được hành động nhạy cảm, nhưng agent có thể bị dẫn dắt trả lời lạc đề.
