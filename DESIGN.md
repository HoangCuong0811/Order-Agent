# Customer Support Agent — Tài liệu thiết kế (bản để duyệt)

Agent chăm sóc khách hàng nhỏ: nhận câu hỏi về đơn hàng, tự xử lý việc nằm trong quyền hạn, và chuyển cho con người duyệt khi vượt quyền. Chạy trên CLI, Python + Gemini API.

## 1. Phạm vi

**Có làm**
- Chat CLI: khách hỏi về đơn hàng, agent tra cứu và xử lý.
- Lệnh `scan`: quét toàn bộ đơn mẫu, gắn cờ đơn có vấn đề, sinh báo cáo tóm tắt kèm đề xuất.
- Human-in-the-loop ngay trên CLI (`y/n`).
- Guardrails nằm trong code (không dựa vào prompt).
- Xử lý input sai/thiếu một cách an toàn.

**Không làm** (để giữ đơn giản): UI, API server, database, mock không cần key, audit log ra file, nhiều khách/nhiều phiên đồng thời.

## 2. Luồng hoạt động

```
Khách nhập tin nhắn
      │
Validate input ──(rỗng / quá dài)──► báo lỗi, yêu cầu nhập lại
      │
Gemini (function calling) ──► đề xuất gọi tool
      │
Policy layer (code, quyết định trước khi tool chạy)
      ├─ AUTO ──────────► chạy tool, trả kết quả cho Gemini
      ├─ NEEDS_APPROVAL ► CLI hiển thị đề xuất, hỏi y/n
      │                     ├─ y: chạy tool
      │                     └─ n: không chạy, báo Gemini "bị từ chối"
      └─ BLOCKED ───────► không chạy, trả lý do cho Gemini (agent giải thích / escalate)
      │
Gemini trả lời: Tóm tắt · Hành động đề xuất · Trạng thái
```

Vòng lặp tool-calling do **mình tự điều khiển** (tắt automatic function calling của SDK) để chèn policy layer vào giữa. Giới hạn tối đa 5 lượt gọi tool cho mỗi tin nhắn để tránh vòng lặp vô hạn.

## 3. Cấu trúc file

```
customer-support-agent/
├── src/                   # toàn bộ code
│   ├── main.py            # CLI: chế độ chat và `scan`
│   ├── agent.py           # Vòng lặp Gemini + gọi policy + hỏi duyệt
│   ├── tools.py           # Các tool: get_order, cancel_order, issue_refund, escalate_to_human
│   └── policy.py          # Luật guardrail + hàm detect_issues (thuần code, không LLM)
├── data/
│   └── orders.json        # Dữ liệu mẫu
├── requirements.txt       # google-genai, python-dotenv
├── .env.example           # GEMINI_API_KEY=, GEMINI_MODEL=
├── README.md
└── DESIGN.md
```

Chạy bằng `python src/main.py` hoặc `python src/main.py scan` từ thư mục gốc.

## 4. Dữ liệu mẫu (`orders.json`)

Khoảng 8 đơn, mỗi đơn có: `order_id` (dạng `ORD-1001`), `customer_name`, `email`, `items`, `total`, `status` (`processing` / `shipped` / `delivered` / `cancelled`), `payment_status` (`paid` / `failed` / `refunded`), `order_date`, `expected_delivery`, `shipping_address`.

Được thiết kế để phủ các tình huống: đơn bình thường, giao trễ, thanh toán lỗi, thiếu địa chỉ, đơn đã giao (khách muốn hoàn tiền), đơn đã huỷ, đơn giá trị lớn. Mọi thay đổi (huỷ, hoàn tiền) chỉ lưu trong bộ nhớ, không ghi đè file.

## 5. Tool và guardrail

| Tool | Điều kiện | Mức |
|---|---|---|
| `get_order(order_id)` | Chỉ đọc | **AUTO** |
| `cancel_order(order_id)` | Đơn `processing` | **AUTO** |
| | Đơn `shipped` | **NEEDS_APPROVAL** |
| | Đơn `delivered` / `cancelled` | **BLOCKED** |
| `issue_refund(order_id, amount, reason)` | `payment_status = paid` và `amount ≤ 50` | **AUTO** |
| | `payment_status = paid` và `amount > 50` | **NEEDS_APPROVAL** |
| | `payment_status ≠ paid`, hoặc `amount ≤ 0`, hoặc `amount > total` | **BLOCKED** |
| `escalate_to_human(summary)` | Yêu cầu ngoài quyền hạn (xoá tài khoản, khiếu nại, đòi bồi thường…) | **AUTO** (chỉ tạo ticket giả lập, in ra màn hình) |

Ngưỡng 50 là hằng số trong `src/policy.py`, dễ đổi.

**Nguyên tắc chính**: LLM không có đường nào tự chạy được hành động nhạy cảm. Policy layer đánh giá dựa trên **dữ liệu thật của đơn**, không dựa vào lời LLM hay lời khách nói. Những yêu cầu không có tool tương ứng (ví dụ xoá tài khoản) thì agent gọi `escalate_to_human`.

## 6. Phát hiện vấn đề (`detect_issues`)

Luật cố định trong code, dùng chung cho chat và `scan`:

- `payment_failed`: `payment_status = failed`
- `late_delivery`: quá `expected_delivery` mà `status` chưa `delivered`
- `missing_address`: địa chỉ giao trống
- `high_value_pending`: đơn `processing` có `total` lớn hơn 500 (cần kiểm tra thủ công)

`scan` chạy `detect_issues` trên mọi đơn, rồi gửi danh sách đơn bị gắn cờ cho Gemini **một lần** để viết tóm tắt và đề xuất cho từng đơn. Chế độ này **chỉ đề xuất, không thực thi hành động nào**.

## 7. Xử lý input không hợp lệ

| Tình huống | Cách xử lý |
|---|---|
| Tin nhắn rỗng hoặc dài hơn 1000 ký tự | Từ chối ngay ở CLI, không gọi LLM |
| Mã đơn sai định dạng (không khớp `ORD-\d{4}`) | Tool trả lỗi, agent hỏi lại khách |
| Mã đơn không tồn tại | Tool trả `not_found`, agent không bịa dữ liệu |
| Khách hỏi về đơn nhưng không cung cấp mã | Agent hỏi lại mã đơn |
| Tham số tool sai kiểu / thiếu (ví dụ `amount` là chữ, âm) | Validate trong tool, trả lỗi, không thực thi |
| Gemini lỗi API (mạng, quota, key sai) | Bắt exception, in thông báo thân thiện, CLI không crash |
| Quá 5 lượt tool call | Dừng, chuyển `escalate_to_human` |
| Thiếu `GEMINI_API_KEY` | Báo lỗi rõ ràng lúc khởi động |

## 8. Định dạng đầu ra mỗi lượt

System prompt yêu cầu Gemini luôn kết thúc câu trả lời bằng 3 phần:

```
Tóm tắt: <vấn đề của khách / tình trạng đơn>
Đề xuất: <hành động tiếp theo>
Trạng thái: ĐÃ XỬ LÝ | CHỜ DUYỆT | ĐÃ ESCALATE | CẦN THÊM THÔNG TIN
```

## 9. Kịch bản demo (đối chiếu với yêu cầu đề bài)

| # | Khách nói | Kết quả mong đợi | Yêu cầu được chứng minh |
|---|---|---|---|
| 1 | "Đơn ORD-1001 của tôi đang ở đâu?" | Tra cứu, trả lời tự động | Phân tích request, tự xử lý |
| 2 | "Huỷ đơn ORD-1002" (đang `processing`) | Huỷ tự động | Hành động an toàn tự chạy |
| 3 | "Hoàn 200$ cho ORD-1004" | CLI hỏi duyệt `y/n` | Human approval |
| 4 | "Huỷ đơn ORD-1005" (đã `delivered`) | BLOCKED, agent giải thích | Guardrail |
| 5 | "Xoá tài khoản của tôi" | Escalate cho người | Vượt quyền hạn |
| 6 | "Đơn của tôi đâu?" (không có mã) / `ORD-9999` / chuỗi rỗng | Hỏi lại / báo không tồn tại / từ chối | Input không hợp lệ |
| 7 | `python src/main.py scan` | Báo cáo các đơn bị gắn cờ + đề xuất | Detect issues, summary, next action |

## 10. Giả định cần bạn xác nhận

1. **Model**: mặc định `gemini-2.5-flash`, đổi được qua biến `GEMINI_MODEL` trong `.env`. Nếu bạn dùng model khác thì chỉ cần sửa biến này.
2. **SDK**: dùng `google-genai` (SDK chính thức mới của Google), không dùng `google-generativeai` đã cũ.
3. **Ngưỡng**: hoàn tiền tự động ≤ 50, cần duyệt khi > 50; cảnh báo đơn giá trị cao khi > 500. Bạn muốn con số khác không?
4. **Ngôn ngữ giao tiếp**: agent trả lời theo ngôn ngữ khách dùng (Việt hoặc Anh); output CLI bằng tiếng Việt.
5. **Dữ liệu**: chỉ lưu trong bộ nhớ, mỗi lần chạy lại thì reset về dữ liệu mẫu.
