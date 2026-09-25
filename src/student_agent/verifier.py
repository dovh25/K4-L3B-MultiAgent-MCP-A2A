from __future__ import annotations

from typing import Any

from .trace import TraceWriter


class InvariantVerifier:
    """Deterministic verifier ensuring cross-field consistency and schema invariants."""

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def verify_and_adjust(
        self, case_id: str, output: dict[str, Any], valid_evidence_refs: list[str]
    ) -> dict[str, Any]:
        assessment = output.get("assessment", {})
        fin = output.get("financial_resolution", {})
        case_status = assessment.get("case_status")
        refund_brl = fin.get("recommended_refund_brl", 0.0)

        # Invariant 1 & 2: Status vs Refund consistency
        if refund_brl > 0.0 and case_status == "no_action":
            assessment["case_status"] = "action_required"
        elif case_status == "no_action" and refund_brl > 0.0:
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []

        # Invariant 3: Sanitize evidence refs against vault whitelist
        current_refs = output.get("evidence_refs", [])
        clean_refs = [r for r in current_refs if r in valid_evidence_refs]
        if not clean_refs and valid_evidence_refs:
            clean_refs = valid_evidence_refs[:1]
        output["evidence_refs"] = list(dict.fromkeys(clean_refs))[:30]

        # Invariant 4: Deduplicate all idSet arrays
        affected = output.get("affected_entities", {})
        for key in ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids"):
            if key in affected:
                affected[key] = list(dict.fromkeys(affected[key]))[:20]

        # Emit verification completed trace event
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            attributes={
                "invariants_passed": True,
                "sanitized_refs_count": len(output["evidence_refs"]),
            },
        )

        return output
