from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from .evidence_vault import EvidenceVault
from .trace import TraceWriter


@dataclass(frozen=True)
class PaymentAnalysisResult:
    verdict: str
    captured_total_brl: float
    refunded_total_brl: float
    refundable_total_brl: float
    payment_references: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "captured_total_brl": round(self.captured_total_brl, 2),
            "refunded_total_brl": round(self.refunded_total_brl, 2),
            "refundable_total_brl": round(self.refundable_total_brl, 2),
        }


class PaymentSpecialist:
    """Specialist agent that audits payment amounts, reconciliation, and refund statuses."""

    def __init__(self, vault: EvidenceVault, trace: TraceWriter) -> None:
        self.vault = vault
        self.trace = trace

    async def analyze(
        self, order_id: str, case_id: str, primary_claim_topic: str
    ) -> PaymentAnalysisResult:
        # 1. Emit task assignment
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
            attributes={"task": "payment_analysis", "order_id": order_id},
        )

        payments: list[dict[str, Any]] = []
        # 2. Fetch payments from MCP
        try:
            res = await self.vault.call(
                "get_order_payments", actor="payment-agent", order_id=order_id
            )
            data = res.get("data", [])
            if isinstance(data, list):
                payments = data
            elif isinstance(data, dict) and "payments" in data:
                payments = data["payments"]
        except Exception:
            pass

        # Also consult payment timeline if payments list is empty
        if not payments:
            try:
                res = await self.vault.call(
                    "get_payment_timeline", actor="payment-agent", order_id=order_id
                )
                data = res.get("data", {})
                payments = data.get("payments", [])
            except Exception:
                pass

        # 3. Compute totals
        captured_total = 0.0
        payment_references: list[str] = []
        for i, p in enumerate(payments, 1):
            try:
                val = float(p.get("payment_value", 0.0))
                captured_total += val
            except (ValueError, TypeError):
                pass
            seq = p.get("payment_sequential", str(i))
            ptype = p.get("payment_type", "payment")
            payment_references.append(f"{ptype}_{order_id[:12]}_{seq}")

        if not payment_references:
            payment_references = [f"pay_{order_id[:16]}"]

        refunded_total = 0.0
        # If the claim is related to refunds, try fetching refund timeline
        if "refund" in primary_claim_topic:
            try:
                res = await self.vault.call(
                    "get_refund_timeline", actor="payment-agent", order_id=order_id
                )
                events = res.get("data", {}).get("events", [])
                for e in events:
                    if e.get("status") in ("confirmed", "completed"):
                        with contextlib.suppress(ValueError, TypeError):
                            refunded_total += float(e.get("amount_brl", 0.0))
            except Exception:
                pass

        captured_total = round(captured_total, 2)
        refunded_total = round(refunded_total, 2)
        refundable_total = max(0.0, round(captured_total - refunded_total, 2))

        # 4. Determine verdict
        if primary_claim_topic == "duplicate_charge":
            verdict = "duplicate_capture"
        elif primary_claim_topic == "payment_mismatch":
            verdict = "capture_mismatch"
        elif primary_claim_topic == "refund_failed":
            verdict = "refund_failed"
        elif primary_claim_topic == "refund_pending":
            verdict = "refund_pending"
        elif refunded_total > 0 and refundable_total == 0.0:
            verdict = "refunded"
        elif captured_total > 0:
            verdict = "reconciled"
        else:
            verdict = "insufficient_evidence"

        # 5. Emit handoff back to coordinator
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment-agent",
            target="coordinator",
            attributes={
                "verdict": verdict,
                "captured_brl": captured_total,
                "refundable_brl": refundable_total,
            },
        )

        return PaymentAnalysisResult(
            verdict=verdict,
            captured_total_brl=captured_total,
            refunded_total_brl=refunded_total,
            refundable_total_brl=refundable_total,
            payment_references=payment_references[:20],
        )
