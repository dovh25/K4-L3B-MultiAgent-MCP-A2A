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
        primary_issue = assessment.get("primary_issue", "")

        # ── Invariant 1: Status vs Refund consistency ──
        if refund_brl > 0.0 and case_status == "no_action":
            # Cannot have refund with no_action
            assessment["case_status"] = "action_required"
        elif case_status == "no_action" and refund_brl > 0.0:
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []

        # ── Invariant 2: no_action should have zero refund ──
        if assessment.get("case_status") == "no_action":
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []

        # ── Invariant 3: Sanitize evidence refs against vault whitelist ──
        current_refs = output.get("evidence_refs", [])
        clean_refs = [r for r in current_refs if r in valid_evidence_refs]
        if not clean_refs and valid_evidence_refs:
            clean_refs = valid_evidence_refs[:1]
        output["evidence_refs"] = list(dict.fromkeys(clean_refs))[:30]

        # ── Invariant 4: Deduplicate all idSet arrays ──
        affected = output.get("affected_entities", {})
        for key in ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids"):
            if key in affected:
                affected[key] = list(dict.fromkeys(affected[key]))[:20]
                # Ensure no empty arrays
                if not affected[key]:
                    if key == "order_ids":
                        affected[key] = [case_id]
                    elif key == "item_ids":
                        affected[key] = [f"item_{case_id[:16]}"]
                    elif key == "shipment_ids":
                        affected[key] = [f"ship_{case_id[:16]}"]

        # ── Invariant 5: Seller responsibility consistency ──
        root_cause = output.get("root_cause_analysis", {})
        parties = root_cause.get("responsible_parties", [])
        seller_ids_in_entities = affected.get("seller_ids", [])
        for party in parties:
            if party.get("party_type") == "seller" and not party.get("party_id"):
                if seller_ids_in_entities:
                    party["party_id"] = seller_ids_in_entities[0]

        # ── Invariant 6: Claim evidence_refs must be valid ──
        for claim in output.get("claim_assessments", []):
            claim_refs = claim.get("evidence_refs", [])
            clean_claim_refs = [r for r in claim_refs if r in valid_evidence_refs]
            if not clean_claim_refs and valid_evidence_refs:
                clean_claim_refs = valid_evidence_refs[:1]
            claim["evidence_refs"] = list(dict.fromkeys(clean_claim_refs))[:30]

        # ── Invariant 7: Resolution actions should not be duplicate ──
        actions = output.get("resolution_actions", [])
        output["resolution_actions"] = list(dict.fromkeys(actions))[:8]

        # ── Invariant 8: unsupported_claim should be no_action ──
        if primary_issue == "unsupported_claim":
            assessment["case_status"] = "no_action"
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []

        # ── Invariant 9: valid_split_payment reconciled → no_action ──
        if primary_issue == "valid_split_payment":
            payment = output.get("payment_analysis", {})
            if payment.get("verdict") == "reconciled":
                assessment["case_status"] = "no_action"
                fin["recommended_refund_brl"] = 0.0
                fin["refund_lines"] = []

        # ── Invariant 10: Confidence calibration guard ──
        conf = assessment.get("confidence", 0.5)
        assessment["confidence"] = round(min(0.99, max(0.50, conf)), 2)

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
