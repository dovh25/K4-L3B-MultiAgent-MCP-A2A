# L3B Architecture Record

Team: Day09 L3B Multi-Agent MCP + A2A System
Tài liệu mô tả kiến trúc hệ thống multi-agent có thể kiểm chứng độc lập.

## 1. System overview

Luồng điều phối tổng thể từ input, phân giải thực thể, điều tra chuyên sâu đến thẩm định bất biến và xuất kết quả:

```text
Input → Entity Resolver → Coordinator → Specialists (Shipment, Payment, Order)
            │                  │                       │
            │                  ├─────────────── Policy & Conflict Engine
            │                  │                       │
            │                  ▼                       │
            └────────── EvidenceVault (MCP) ───────────┴──── TraceWriter (Trace.jsonl)
                               │
                        Invariant Verifier
                               │
                             Output (outputs/<case_id>.json)
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | `candidate_order_ids`, `customer_unique_id_hint` | Lọc candidate rác, xác thực qua lịch sử khách hàng | `get_customer_history`, `get_order` | `EntityResolutionResult` ➔ Coordinator |
| Coordinator | `case` payload | Tiếp nhận case, điều phối phân công nhiệm vụ, tổng hợp output | `get_order_items` | Điều phối Specialists ➔ Verifier |
| Order/product | `order_id` | Truy vấn danh mục item, mã sản phẩm và định danh người bán | `get_order_items`, `get_sellers` | Item IDs, Seller IDs ➔ Coordinator |
| Shipment | `order_id`, `primary_claim_topic` | Phân tích mốc vận chuyển, trễ hạn giao carrier, sự kiện giao trễ | `get_shipment_summary`, `get_order` | `ShipmentAnalysisResult` ➔ Coordinator |
| Payment/refund | `order_id`, `primary_claim_topic` | Đối soát tổng tiền thu, tiền hoàn, phát hiện duplicate capture | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `PaymentAnalysisResult` ➔ Coordinator |
| Policy | `policy_version`, `primary_topic` | Đối chiếu quy tắc chính sách, xác định khoản hoàn tiền và trách nhiệm | `get_policy` | `PolicyEvaluationResult` ➔ Coordinator |
| LLM Reasoner (`allam-2-7b`) | `customer_request.message`, `primary_topic` | Suy luận ngữ nghĩa tin nhắn khách hàng, trích xuất yêu cầu cốt lõi | Không (Cloud LLM API ≤ 10B) | Semantic Summary ➔ Coordinator |
| Conflict resolver | Dữ liệu từ các specialists | Phát hiện và phân giải xung đột dữ liệu giữa các nguồn | Không (Symbolic logic) | `data_conflicts` ➔ Coordinator |
| Verifier | Draft output object | Kiểm định tính toàn vẹn schema, invariants và whitelist evidence refs | Không (Invariants checking) | Validated Output ➔ Finalize |

## 3. Entity resolution và A2A protocol

- **Phân loại Candidate**: Tách biệt candidate giả lập (`candidate-xxx` hoặc không thỏa mãn định dạng hexadecimal 32 ký tự MD5) và đưa ngay vào `rejected_candidates`.
- **Đối chiếu lịch sử**: Dùng `customer_unique_id_hint` gọi `get_customer_history` một lần duy nhất để kiểm tra `claimed_order_id` có tồn tại trong danh sách đơn hàng đã mua của khách hay không.
- **Độ tin cậy (Confidence)**:
  - `1.0`: Khớp hoàn toàn với đơn hàng trong lịch sử khách hàng authoritative.
  - `0.95`: Xác nhận trực tiếp thành công qua `get_order`.
  - `0.75`: Không tìm thấy hoặc dữ liệu mơ hồ.
- **A2A Protocol**:
  - Giao tiếp có correlation theo `case_id`.
  - Mỗi bước chuyển giao đều phát sinh sự kiện tường minh: `task_assigned` ➔ `tool_result_consumed` ➔ `handoff`.
  - Tuyệt đối không đưa nội dung prompt bí mật hay chuỗi suy luận riêng (chain-of-thought) vào trace.

## 4. Evidence và conflict lifecycle

- **EvidenceVault**:
  - Toàn bộ cuộc gọi MCP đều đi qua `EvidenceVault`.
  - Cơ chế cache cục bộ trong phạm vi case: chỉ gọi MCP một lần cho mỗi bộ tham số `(tool_name, arguments)`, giúp tiết kiệm tối đa ngân sách cuộc gọi (bảo vệ 5% điểm `efficiency`).
  - Ghi nhận `evidence_ref`, `domain` và phát sinh ngay sự kiện `tool_result_consumed` để bảo đảm tính liên kết trace-to-evidence.
  - Không tái sử dụng `evidence_ref` chéo case (tránh vi phạm Hard Gate `cross_scope_evidence_ref`).
- **Phân giải xung đột (Conflict Resolution)**:
  - Khi có mâu thuẫn giữa trạng thái đơn tĩnh (`order_status_record`) và dòng sự kiện vận chuyển thực tế (`shipment_carrier_events`), hệ thống ưu tiên nhật ký sự kiện có xác nhận của đơn vị vận chuyển (`prefer_authoritative_carrier_events`).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / network error | 1 | Sử dụng default an toàn cho domain, không gọi lại | `tool_result_consumed` với fallback |
| Entity not found / ambiguous | 0 | Đánh dấu status `not_found`, confidence = 0.75 | `handoff` (status: `not_found`) |
| Source conflict | 0 | Ưu tiên authoritative audit events theo policy | Ghi nhận vào `data_conflicts` |
| Invalid specialist result | 0 | Áp dụng giá trị an toàn (`on_time`, `reconciled`) | InvariantVerifier điều chỉnh |

- **Ngân sách cuộc gọi**: Trung bình mỗi case chỉ gọi từ 3 đến 5 công cụ MCP thiết yếu (`get_customer_history`, `get_shipment_summary`, `get_order`, `get_order_payments`, `get_policy`), không bao giờ quét thừa các công cụ ngoài phạm vi claim.

## 6. Verification invariants

Trước khi xuất file output, `InvariantVerifier` thực hiện các kiểm định bắt buộc:
1. **Status vs Refund**:
   - Nếu `recommended_refund_brl > 0.0` ➔ Bắt buộc `case_status == "action_required"`.
   - Nếu `case_status == "no_action"` ➔ Bắt buộc `recommended_refund_brl == 0.0` và `refund_lines == []`.
2. **Evidence Ownership**:
   - Mọi `evidence_ref` trong output phải nằm trong whitelist bằng chứng thật được trả về từ MCP Gateway của case đó.
3. **IdSet Boundaries**:
   - Toàn bộ các mảng định danh (`order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids`, `resolved_order_ids`, `rejected_candidates`) đều được khử trùng lặp (`uniqueItems: true`) và giới hạn tối đa 20 phần tử.
4. **Trace Lifecycle Completeness**:
   - Đảm bảo có đầy đủ 5 sự kiện cốt lõi: `case_received`, `task_assigned`, `handoff`, `verification_completed`, `case_finalized`.

## 7. Reproducibility

- **Môi trường chạy**: Python 3.12+ (với `setuptools`, `httpx2`, `mcp`, `jsonschema`, `python-dotenv`).
- **Mô hình suy luận LLM**: `allam-2-7b` (7B tham số, thỏa mãn điều kiện ≤ 10B) qua REST API OpenAI-compatible với cơ chế fallback tự động.
- **Kiến trúc Multi-Agent**: 7 chuyên viên (`entity-agent`, `shipment-agent`, `payment-agent`, `policy-agent`, `llm-reasoner-agent`, `verifier`, `coordinator`).
- **Lệnh thực thi**:
  - `day09 validate-inputs`: Kiểm định 100 cases đầu vào.
  - `day09 run`: Chạy toàn bộ pipeline điều tra cho 100 cases.
  - `day09 validate`: Thẩm định tính hợp lệ của toàn bộ output và trace.
  - `day09 package --output dist/submission.zip`: Đóng gói bài nộp hoàn chỉnh.
