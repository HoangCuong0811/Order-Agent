# Order Support Chatbot — Tài liệu thiết kế

Chatbot hỗ trợ đơn hàng mô phỏng một sàn thương mại điện tử: nhận câu hỏi về đơn hàng, tự xử lý việc nằm trong quyền hạn, và chuyển cho con người duyệt khi vượt quyền. Python + FastAPI + Gemini API, kèm giao diện chat chạy trên trình duyệt.

## 1. Phạm vi

**Có làm**
- REST API cho hội thoại: tạo phiên, gửi tin nhắn, duyệt/từ chối hành động.
- Giao diện chat tối giản (một trang tĩnh, nền đen); người dùng đóng cả vai khách hàng và nhân viên CSKH (nút Duyệt / Từ chối hiện ngay trong chat).
- Duyệt bởi con người (human-in-the-loop) qua API, không chặn thread.
- Guardrails nằm trong code (không dựa vào prompt).
- Xử lý input sai/thiếu một cách an toàn.

**Không làm** (để giữ đơn giản): xác thực khách hàng/nhân viên, database, lưu phiên qua lần khởi động lại, nhiều process, streaming token.

**Đã bỏ**: chế độ CLI và lệnh `scan` (quét toàn bộ đơn, sinh báo cáo). `detect_issues` vẫn còn, dùng cho tool `get_order`.

## 2. Luồng hoạt động

```
Trình duyệt ──HTTP──► FastAPI ──► SupportAgent (một đối tượng cho mỗi phiên)
                                      │
Validate input ──(rỗng / quá dài)──► 400, không gọi LLM
      │
Gemini (function calling) ──► đề xuất gọi tool
      │
Policy layer (code, quyết định trước khi tool chạy)
      ├─ AUTO ──────────► chạy tool, trả kết quả cho Gemini
      ├─ NEEDS_APPROVAL ► lưu trạng thái lượt, trả status "pending_approval"
      │                     ├─ POST /approval {approved: true}: kiểm tra lại policy, chạy tool, chạy tiếp
      │                     └─ POST /approval {approved: false}: không chạy, báo Gemini "bị từ chối"
      └─ BLOCKED ───────► không chạy, trả lý do cho Gemini (agent giải thích / escalate)
      │
Gemini trả lời: Tóm tắt · Đề xuất · Trạng thái
```

Vòng lặp tool-calling do **mình tự điều khiển** (tắt automatic function calling của SDK) để chèn policy layer vào giữa. Giới hạn tối đa 5 lượt gọi Gemini cho mỗi tin nhắn để tránh vòng lặp vô hạn.

### Tạm dừng và tiếp tục khi chờ duyệt

Trên CLI trước đây, bước duyệt là `input()` chặn luồng. Với API, một request không thể chờ nhân viên, nên lượt hội thoại được tách thành các bước có thể dừng:

- Agent giữ trong phiên: danh sách tool call Gemini đề xuất ở bước hiện tại, các kết quả đã có (theo đúng thứ tự), và yêu cầu đang chờ duyệt.
- Gặp tool `NEEDS_APPROVAL`: dừng, `chat` trả `TurnResult(status="pending_approval")`.
- `resolve_approval(approved)`: xử lý tool đó (chạy hoặc từ chối), chạy nốt các tool còn lại của bước (có thể lại dừng nếu có tool khác cần duyệt), rồi gọi Gemini tiếp.
- Khi duyệt, policy được **đánh giá lại** trên dữ liệu hiện tại, vì phiên khác có thể đã đổi đơn trong lúc chờ.
- Lỗi ở bất kỳ bước nào: lịch sử về trước lượt đó, xoá trạng thái chờ, phiên dùng tiếp được.

## 3. Cấu trúc file

```
├── src/
│   ├── main.py            # khởi động server (uvicorn)
│   ├── api.py             # REST API, quản lý phiên, phục vụ UI
│   ├── agent.py           # Vòng lặp Gemini + gọi policy + tạm dừng/tiếp tục khi chờ duyệt
│   ├── tools.py           # get_order, cancel_order, issue_refund, escalate_to_human
│   ├── policy.py          # Luật guardrail + hàm detect_issues (thuần code, không LLM)
│   ├── agent_trace.py     # Ghi vết từng bước ra logs/agent_trace.log
│   └── static/index.html  # Giao diện chat
├── data/orders.json       # Dữ liệu mẫu
├── requirements.txt       # google-genai, python-dotenv, fastapi, uvicorn
├── .env.example           # GEMINI_API_KEY=, GEMINI_MODEL=
├── README.md
└── DESIGN.md
```

Chạy bằng `python src/main.py` từ thư mục gốc, mở `http://127.0.0.1:8000`.

## 4. API

| Method | Đường dẫn | Mô tả |
|---|---|---|
| `POST` | `/api/sessions` | Tạo phiên, trả `session_id` |
| `POST` | `/api/sessions/{id}/messages` | Gửi tin nhắn của khách |
| `POST` | `/api/sessions/{id}/approval` | Nhân viên duyệt / từ chối |
| `POST` | `/api/demo/reset-orders` | Nạp lại dữ liệu đơn mẫu |
| `GET` | `/api/health` | Kiểm tra server |

`messages` và `approval` trả cùng một dạng: `status` (`reply` hoặc `pending_approval`), `reply` (toàn văn), `body` (bỏ 3 dòng cuối), `summary` / `suggestion` / `case_status` (tách từ 3 dòng cuối), `approval` (hành động chờ duyệt, nếu có), `tool_events` (các tool đã gọi cùng quyết định của policy; UI hiện tại không hiển thị, dành cho client khác hoặc debug).

| Mã | Khi nào |
|---|---|
| 400 | Tin nhắn rỗng hoặc dài hơn 1000 ký tự |
| 404 | Phiên không tồn tại (kể cả sau khi server khởi động lại) |
| 409 | Gửi tin khi đang chờ duyệt, duyệt khi không có gì chờ, hoặc phiên đang xử lý lượt trước |

**Phiên** lưu trong bộ nhớ, tối đa 200 (vượt thì bỏ phiên cũ nhất). Mỗi phiên có một lock; request thứ hai vào cùng phiên khi đang bận nhận 409 thay vì chạy song song trên cùng lịch sử. Endpoint dùng `def` thường vì gọi Gemini là blocking, FastAPI chạy chúng trong threadpool.

## 5. Dữ liệu mẫu (`orders.json`)

9 đơn, mỗi đơn có: `order_id` (dạng `ORD-1001`), `customer_name`, `email`, `items`, `total`, `status` (`processing` / `shipped` / `delivered` / `cancelled`), `payment_status` (`paid` / `failed` / `refunded`), `order_date`, `expected_delivery`, `shipping_address`.

Phủ các tình huống: đơn bình thường, giao trễ, thanh toán lỗi, thiếu địa chỉ, đơn đã giao (khách muốn hoàn tiền), đơn đã huỷ, đơn giá trị lớn. Mọi thay đổi (huỷ, hoàn tiền) chỉ lưu trong bộ nhớ, dùng chung cho mọi phiên, và về mẫu khi khởi động lại hoặc gọi `/api/demo/reset-orders`.

## 6. Tool và guardrail

| Tool | Điều kiện | Mức |
|---|---|---|
| `get_order(order_id)` | Chỉ đọc | **AUTO** |
| `cancel_order(order_id)` | Đơn `processing` | **AUTO** |
| | Đơn `shipped` | **NEEDS_APPROVAL** |
| | Đơn `delivered` / `cancelled` | **BLOCKED** |
| `issue_refund(order_id, amount, reason)` | `payment_status = paid` và tổng hoàn ≤ 50 | **AUTO** |
| | `payment_status = paid` và tổng hoàn > 50 | **NEEDS_APPROVAL** |
| | `payment_status ≠ paid`, hoặc `amount ≤ 0`, hoặc vượt số tiền còn hoàn được | **BLOCKED** |
| `escalate_to_human(summary)` | Yêu cầu ngoài quyền hạn (xoá tài khoản, khiếu nại, đòi bồi thường…) | **AUTO** (chỉ tạo ticket giả lập, in ra console) |

Ngưỡng 50 là hằng số trong `src/policy.py`, dễ đổi.

**Nguyên tắc chính**: LLM không có đường nào tự chạy được hành động nhạy cảm. Policy layer đánh giá dựa trên **dữ liệu thật của đơn**, không dựa vào lời LLM hay lời khách nói. Những yêu cầu không có tool tương ứng (ví dụ xoá tài khoản) thì agent gọi `escalate_to_human`.

## 7. Phát hiện vấn đề (`detect_issues`)

Luật cố định trong code, `get_order` trả kèm kết quả để agent tham khảo khi trả lời:

- `payment_failed`: `payment_status = failed`
- `late_delivery`: quá `expected_delivery` mà `status` chưa `delivered`
- `missing_address`: địa chỉ giao trống
- `high_value_pending`: đơn `processing` có `total` lớn hơn 500 (cần kiểm tra thủ công)

## 8. Xử lý input không hợp lệ

| Tình huống | Cách xử lý |
|---|---|
| Tin nhắn rỗng hoặc dài hơn 1000 ký tự | API trả 400, không gọi LLM |
| Mã đơn sai định dạng (không khớp `ORD-\d{4}`) | Tool trả lỗi, agent hỏi lại khách |
| Mã đơn không tồn tại | Tool trả `not_found`, agent không bịa dữ liệu |
| Khách hỏi về đơn nhưng không cung cấp mã | Agent hỏi lại mã đơn |
| Tham số tool sai kiểu / thiếu (ví dụ `amount` là chữ, âm) | Validate trong policy/tool, trả lỗi, không thực thi |
| Gemini lỗi API (mạng, quota, key sai) | Bắt exception, trả thông báo thân thiện, khôi phục lịch sử, phiên dùng tiếp được |
| Quá 5 lượt gọi Gemini | Dừng, tự escalate |
| Thiếu `GEMINI_API_KEY` | `main.py` báo lỗi rõ ràng lúc khởi động |
| Gửi tin khi đang chờ duyệt / phiên đang bận | API trả 409 |

## 9. Định dạng đầu ra mỗi lượt

System prompt yêu cầu Gemini luôn kết thúc câu trả lời bằng 3 dòng:

```
Tóm tắt: <vấn đề của khách / tình trạng đơn>
Đề xuất: <hành động tiếp theo>
Trạng thái: ĐÃ XỬ LÝ | CHỜ DUYỆT | ĐÃ ESCALATE | CẦN THÊM THÔNG TIN
```

API tách 3 dòng này thành `summary` / `suggestion` / `case_status` cho client nào cần. UI hiện tại hiển thị nguyên văn `reply`. Nếu model sai định dạng, các trường này là `null` và `body` là toàn văn.

## 10. Kịch bản demo

| # | Khách nói | Kết quả mong đợi | Yêu cầu được chứng minh |
|---|---|---|---|
| 1 | "Đơn ORD-1001 của tôi đang ở đâu?" | Tra cứu, trả lời tự động | Phân tích request, tự xử lý |
| 2 | "Huỷ đơn ORD-1002" (đang `processing`) | Huỷ tự động | Hành động an toàn tự chạy |
| 3 | "Hoàn 200$ cho ORD-1004" | Thẻ "Cần nhân viên duyệt", bấm Duyệt/Từ chối | Human approval |
| 4 | "Huỷ đơn ORD-1005" (đã `delivered`) | BLOCKED, agent giải thích | Guardrail |
| 5 | "Xoá tài khoản của tôi" | Escalate cho người | Vượt quyền hạn |
| 6 | "Đơn của tôi đâu?" / `ORD-9999` | Hỏi lại / báo không tồn tại | Input không hợp lệ |

## 11. Giả định cần xác nhận

1. **Model**: mặc định trong code là `gemini-3.5-flash-lite`, đổi được qua biến `GEMINI_MODEL` trong `.env`.
2. **SDK**: dùng `google-genai` (SDK chính thức mới của Google), không dùng `google-generativeai` đã cũ.
3. **Ngưỡng**: hoàn tiền tự động ≤ 50, cần duyệt khi > 50; cảnh báo đơn giá trị cao khi > 500.
4. **Ngôn ngữ giao tiếp**: agent trả lời theo ngôn ngữ khách dùng (Việt hoặc Anh); giao diện bằng tiếng Việt.
5. **Dữ liệu**: chỉ lưu trong bộ nhớ, dùng chung cho mọi phiên, reset khi khởi động lại.
6. **Không xác thực**: ai cũng gọi được endpoint duyệt. Chấp nhận được cho bản mô phỏng; bản thật phải tách quyền nhân viên.
