# Order Support Chatbot

Chatbot hỗ trợ đơn hàng mô phỏng một sàn thương mại điện tử (Python + FastAPI + Gemini API). Agent nhận câu hỏi về đơn hàng và tự tra cứu thông tin; **mọi thao tác động tới tiền (huỷ đơn, hoàn tiền) luôn cần nhân viên duyệt trước khi chạy, bất kể số tiền**; yêu cầu vượt quyền thì escalate cho người. Có REST API và một giao diện chat tối giản (nền đen) chạy trên trình duyệt.

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
| `HOST`, `PORT` | Địa chỉ và cổng của server. Mặc định `localhost:8000` (nghe cả IPv4 `127.0.0.1` và IPv6 `::1`, chỉ trong máy). Không bắt buộc. |

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
| 2 | `Huỷ đơn ORD-1002` (đang `processing`) | Hiện thẻ "Cần nhân viên duyệt"; chỉ huỷ khi bạn bấm Duyệt |
| 3 | `Hoàn 200$ cho đơn ORD-1004` | Hiện thẻ "Cần nhân viên duyệt", bạn bấm Duyệt hoặc Từ chối |
| 4 | `Huỷ đơn ORD-1005` (đã `delivered`) | Bị chặn, agent giải thích lý do |
| 5 | `Hoàn 30$ cho ORD-1005` | Vẫn hiện thẻ duyệt dù số tiền nhỏ: hoàn tiền luôn cần duyệt |
| 6 | `Xoá tài khoản của tôi` | Agent escalate cho người |
| 7 | `Đơn của tôi đâu?` / `ORD-9999` | Hỏi lại mã đơn / báo không tồn tại |

Giao diện chỉ hiển thị câu trả lời và thẻ duyệt. Các tool agent đã gọi cùng quyết định của guardrail (`AUTO` / `NEEDS_APPROVAL` / `BLOCKED`) nằm trong trường `tool_events` của API, và được in từng bước ra console của backend (xem mục "Theo dõi từng bước của agent").

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
    {"tool": "cancel_order", "args": {"order_id": "ORD-1002"}, "level": "NEEDS_APPROVAL",
     "reason": "huỷ đơn luôn cần nhân viên duyệt", "outcome": "approved"}
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

## Theo dõi từng bước của agent

Mỗi lần có tin nhắn, backend **in ra console** (nơi bạn chạy `python src/main.py`) toàn bộ đường đi của lượt hội thoại, không ghi file. Mỗi bước có số thứ tự `#NN`, mã phiên 8 ký tự, số lượt `L`, và thời gian tính từ đầu lượt. Các bước theo đúng thứ tự hệ thống chạy:

| Bước | Nội dung in ra |
|---|---|
| `NHẬN CÂU HỎI TỪ KHÁCH` | Nội dung tin nhắn, số message lịch sử đã có |
| `GỌI LLM (lần n/5)` | Model, tools khai báo, các message đang gửi cho LLM |
| `LLM TRẢ VỀ` | Thời gian, token, các phần LLM trả về (`function_call` hoặc `text`), và kết luận LLM gọi tool hay trả lời cuối |
| `POLICY KIỂM TRA TOOL` | Tham số LLM đề xuất, dữ liệu đơn policy dựa vào, quyết định `AUTO` / `NEEDS_APPROVAL` / `BLOCKED` và hệ quả |
| `TẠM DỪNG CHỜ NHÂN VIÊN DUYỆT` | Yêu cầu đang chờ duyệt (chỉ khi `NEEDS_APPROVAL`) |
| `NHÂN VIÊN ĐỒNG Ý / TỪ CHỐI` | Quyết định, thời gian chờ, kiểm tra lại policy |
| `THỰC THI TOOL` | Tham số, kết quả tool trả về, thời gian chạy |
| `GỬI KẾT QUẢ TOOL LẠI CHO LLM` | Kết quả từng tool được thêm vào lịch sử để LLM tổng hợp |
| `TRẢ LỜI KHÁCH` | Câu trả lời cuối, tổng thời gian, số lần gọi LLM và tool |

Ngoài ra có các dòng cấp server / phiên: `SERVER SẴN SÀNG`, `PHIÊN MỚI`, `TIN NHẮN BỊ TỪ CHỐI (400)`, `YÊU CẦU BỊ TỪ CHỐI (409)`, và `LỖI ...` kèm traceback khi có exception.

Ví dụ rút gọn cho "Hoàn 200 cho đơn ORD-1004":

```
[20:17:38.952] [e6399237 L1 #01 +0.00s] NHẬN CÂU HỎI TỪ KHÁCH
    Nội dung: "Hoàn 200 cho đơn ORD-1004"
[20:17:38.952] [e6399237 L1 #02 +0.00s] GỌI LLM (lần 1/5)
[20:17:38.952] [e6399237 L1 #03 +0.00s] LLM TRẢ VỀ
      function_call issue_refund({"order_id": "ORD-1004", "amount": 200, "reason": "khách yêu cầu"})
    → LLM yêu cầu gọi 1 tool: issue_refund
[20:17:38.964] [e6399237 L1 #04 +0.00s] POLICY KIỂM TRA TOOL issue_refund
    Dữ liệu policy dựa vào: đơn ORD-1004: trạng thái=delivered, thanh toán=paid, tổng=249.0, đã hoàn=0.0
    Quyết định: NEEDS_APPROVAL — hoàn tiền luôn cần nhân viên duyệt, bất kể số tiền
[20:17:38.964] [e6399237 L1 #05 +0.00s] TẠM DỪNG CHỜ NHÂN VIÊN DUYỆT
[20:17:38.965] [e6399237 L1 #06 +0.00s] NHÂN VIÊN ĐỒNG Ý
[20:17:38.965] [e6399237 L1 #08 +0.00s] THỰC THI TOOL issue_refund (đã được nhân viên duyệt)
[20:17:38.967] [e6399237 L1 #09 +0.00s] GỬI KẾT QUẢ TOOL LẠI CHO LLM
[20:17:38.967] [e6399237 L1 #10 +0.00s] GỌI LLM (lần 2/5)
[20:17:38.967] [e6399237 L1 #11 +0.00s] LLM TRẢ VỀ
    → LLM không gọi tool, đây là câu trả lời cuối (tổng hợp từ kết quả tool ở các bước trước)
[20:17:38.967] [e6399237 L1 #12 +0.00s] TRẢ LỜI KHÁCH
```

Nhiều phiên chạy song song vẫn đọc được: mỗi bước được in liền một khối và có mã phiên, nên lọc bằng mã phiên là ra riêng từng cuộc trò chuyện. Trace nằm trong `src/agent_trace.py`; muốn in thêm bước nào thì gọi `self.trace.step(tiêu_đề, nội_dung)` trong `src/agent.py`.

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
      ├─ AUTO ──────────► (chỉ tra cứu và escalate) chạy tool, trả kết quả cho Gemini
      ├─ NEEDS_APPROVAL ► (mọi huỷ đơn / hoàn tiền) tạm dừng lượt, API trả status "pending_approval"
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
│   ├── agent_trace.py   # in từng bước của agent ra console backend
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
| `cancel_order` | Đơn `processing` hoặc `shipped` | **NEEDS_APPROVAL** (luôn cần duyệt) |
| | Đơn `delivered` / `cancelled` (hoặc mã đơn sai) | BLOCKED |
| `issue_refund` | Đơn đã thanh toán, số tiền hợp lệ và không vượt số còn hoàn được | **NEEDS_APPROVAL** (luôn cần duyệt, bất kể số tiền) |
| | Chưa thanh toán / số tiền ≤ 0 / vượt số tiền còn hoàn được / mã đơn sai | BLOCKED |
| `escalate_to_human` | Luôn cho phép | AUTO |

**Quy tắc cốt lõi: không có đường tự động cho thao tác động tới tiền.** Huỷ đơn và hoàn tiền luôn qua nhân viên duyệt, không phụ thuộc trạng thái đơn hay số tiền. Chỉ tra cứu và escalate chạy tự động. `BLOCKED` áp dụng cho yêu cầu không thể thực hiện, nên không hỏi nhân viên.

### Luật phát hiện vấn đề (`detect_issues`)

`get_order` trả kèm danh sách vấn đề của đơn để agent tham khảo: `payment_failed` (thanh toán lỗi), `missing_address` (thiếu địa chỉ), `late_delivery` (quá hạn giao mà chưa giao), `high_value_pending` (đơn đang xử lý trên 500).

## Key design decisions

1. **Guardrail nằm trong code, không nằm trong prompt.** Prompt chỉ dặn Gemini cách cư xử, nhưng quyền chạy một hành động do `src/policy.py` quyết định dựa trên dữ liệu thật của đơn. Gemini không thể lách bằng cách diễn đạt khác, và khách nói gì cũng không đổi được kết quả.
2. **Tự điều khiển vòng lặp tool-calling.** Tắt automatic function calling của SDK để chèn policy và bước duyệt vào giữa "Gemini đề xuất" và "tool chạy".
3. **Duyệt bất đồng bộ qua API.** Agent không chặn thread để chờ người. Khi cần duyệt, trạng thái lượt (các tool call đang xử lý, kết quả đã có) được giữ trong phiên và `resolve_approval` chạy tiếp đúng từ chỗ dừng. Khi duyệt, policy được **đánh giá lại** vì dữ liệu có thể đã đổi trong lúc chờ (ví dụ phiên khác đã huỷ cùng đơn).
4. **Mặc định chặn (fail closed).** Tool lạ, tham số sai hoặc không đọc được thì bị `BLOCKED`. Không có quyết định duyệt thì tool không chạy.
5. **Mọi thao tác động tới tiền luôn cần người duyệt.** Huỷ đơn và hoàn tiền không có ngưỡng và không có đường tự động. Chỉ tra cứu và escalate tự chạy. Tổng tiền đã hoàn của đơn vẫn được tính để chặn hoàn vượt số tiền của đơn.
6. **Mỗi phiên xử lý một lượt tại một thời điểm.** Gửi tin khi phiên đang bận hoặc đang chờ duyệt trả `409`, để lịch sử hội thoại không bị ghi chồng.
7. **Giới hạn 5 lượt gọi Gemini mỗi tin nhắn.** Vượt thì dừng và tự escalate để tránh vòng lặp vô hạn.
8. **Lỗi không làm hỏng phiên.** Lỗi API được bắt, trả thông báo thân thiện, và lịch sử hội thoại được khôi phục về trước lượt lỗi.
9. **Ngày "hôm nay" cố định** (`policy.TODAY = 2026-10-07`) để dữ liệu mẫu cho kết quả `late_delivery` giống nhau mỗi lần demo.

## Known limitations

- **Không xác thực khách hàng.** Ai biết mã đơn đều xem và thao tác được đơn đó, và ai cũng gọi được `/approval`, `/api/demo/reset-orders`. Bản thật cần đăng nhập khách hàng, và endpoint duyệt phải dành riêng cho nhân viên đã xác thực.
- **Dữ liệu và phiên chỉ lưu trong bộ nhớ.** Huỷ đơn, hoàn tiền, lịch sử hội thoại và yêu cầu đang chờ duyệt mất khi server khởi động lại. Dữ liệu đơn dùng chung cho mọi phiên. Server giữ tối đa 200 phiên gần nhất, chạy một process.
- **Hoàn tiền và escalate chỉ giả lập.** Không gọi cổng thanh toán hay hệ thống ticket thật; `escalate_to_human` chỉ in ra console.
- **Huỷ đơn không tự hoàn tiền.** Hoàn tiền là một thao tác riêng, đi qua guardrail riêng.
- **Lượt lỗi không hoàn tác tool đã chạy.** Nếu một tool đã thực thi rồi Gemini mới lỗi, lịch sử được khôi phục nhưng thay đổi dữ liệu vẫn còn. Tổng tiền đã hoàn được tính vào guardrail nên không thể hoàn vượt số tiền của đơn, nhưng khách có thể phải hỏi lại.
- **Phụ thuộc vào Gemini.** Cần mạng và API key hợp lệ; chất lượng câu trả lời và việc tuân thủ định dạng 3 dòng cuối phụ thuộc model. Guardrail vẫn an toàn dù model trả lời sai định dạng.
- **Luật là hằng số** trong `src/policy.py` (ngưỡng gắn cờ đơn giá trị cao 500, chỉ để cảnh báo), chưa cấu hình được từ ngoài.
- **Chưa có test tự động trong repo** và chưa kiểm thử với nhiều model Gemini khác nhau.
- **Chưa có cơ chế chống prompt injection riêng.** Guardrail vẫn chặn được hành động nhạy cảm, nhưng agent có thể bị dẫn dắt trả lời lạc đề.
