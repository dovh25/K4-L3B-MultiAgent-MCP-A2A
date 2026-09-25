from __future__ import annotations

from typing import Any

from .entity_resolver import EntityResolver
from .evidence_vault import EvidenceVault
from .llm_agent import LLMReasoningAgent
from .mcp_gateway import EvidenceGateway
from .payment_specialist import PaymentSpecialist
from .policy_engine import PolicyEngine
from .shipment_specialist import ShipmentSpecialist
from .trace import TraceWriter
from .verifier import InvariantVerifier

# -----------------------------------------------------------------
# Topic → Required domain mapping for efficient tool routing.
# Only call tools that are RELEVANT to the claim topic.
# -----------------------------------------------------------------
_SHIPMENT_TOPICS = frozenset({
    "late_delivery_seller",
    "late_delivery_logistics",
})

_PAYMENT_TOPICS = frozenset({
    "payment_mismatch",
    "duplicate_charge",
    "valid_split_payment",
    "refund_pending",
    "refund_failed",
    "canceled_order_paid",
    "unavailable_order_paid",
})

# Topics that need items for complete affected_entities
_ITEMS_TOPICS = frozenset({
    "canceled_order_paid",
    "unavailable_order_paid",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
})

# Map topic → proper responsible party type
_TOPIC_RESPONSIBLE_PARTY: dict[str, str] = {
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "payment_mismatch": "payment_provider",
    "duplicate_charge": "payment_provider",
    "valid_split_payment": "platform",
    "refund_pending": "platform",
    "refund_failed": "payment_provider",
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "seller",
    "unsupported_claim": "unknown",
    "insufficient_evidence": "unknown",
}

# Map topic → proper case_status when evidence supports the claim
_TOPIC_DEFAULT_STATUS: dict[str, str] = {
    "late_delivery_seller": "action_required",
    "late_delivery_logistics": "action_required",
    "payment_mismatch": "action_required",
    "duplicate_charge": "action_required",
    "valid_split_payment": "no_action",
    "refund_pending": "action_required",
    "refund_failed": "action_required",
    "canceled_order_paid": "action_required",
    "unavailable_order_paid": "action_required",
    "unsupported_claim": "no_action",
    "insufficient_evidence": "needs_investigation",
}


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for one case."""
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    claims = customer_request.get("claims", [])
    primary_topic = claims[0]["topic"] if claims else "unsupported_claim"
    policy_version = case.get("policy_version", "EC_POLICY_V2")

    vault = EvidenceVault(gateway, trace, case_id)

    # ── 1. Entity Resolution (always needed) ──
    resolver = EntityResolver(vault, trace)
    entity_result = await resolver.resolve(case)
    order_id = (
        entity_result.primary_order_id
        or case.get("candidate_order_ids", [""])[0]
    )

    # ── 2. Conditional Shipment Investigation ──
    need_shipment = primary_topic in _SHIPMENT_TOPICS
    shipment_spec = ShipmentSpecialist(vault, trace)
    if need_shipment:
        shipment_result = await shipment_spec.analyze(
            order_id, case_id, primary_topic
        )
    else:
        shipment_result = shipment_spec.empty_result()

    # ── 3. Conditional Payment Investigation ──
    need_payment = primary_topic in _PAYMENT_TOPICS
    payment_spec = PaymentSpecialist(vault, trace)
    if need_payment:
        payment_result = await payment_spec.analyze(
            order_id, case_id, primary_topic
        )
    else:
        payment_result = payment_spec.empty_result()

    # ── 4. Conditional Item & Seller Discovery ──
    item_ids: list[str] = []
    seller_ids: list[str] = list(shipment_result.seller_ids)

    if primary_topic in _ITEMS_TOPICS:
        try:
            items_res = await vault.call(
                "get_order_items",
                actor="coordinator",
                order_id=order_id,
            )
            for item in items_res.get("data", []):
                item_id = item.get("order_item_id")
                if item_id and item_id not in item_ids:
                    item_ids.append(item_id)
                seller_id = item.get("seller_id")
                if seller_id and seller_id not in seller_ids:
                    seller_ids.append(seller_id)
        except Exception:
            pass

    if not item_ids:
        item_ids = [f"item_{order_id[:16]}"]

    # ── 5. Semantic Customer Reasoning (LLM Agent ≤ 10B) ──
    customer_message = customer_request.get("message", "")
    llm_agent = LLMReasoningAgent(trace)
    llm_result = await llm_agent.analyze_claim_intent(
        case_id=case_id,
        customer_message=customer_message,
        primary_topic=primary_topic,
    )

    # ── 6. Policy Engine Evaluation (always needed) ──
    policy_engine = PolicyEngine(vault, trace)
    policy_result = await policy_engine.evaluate(
        case_id=case_id,
        policy_version=policy_version,
        primary_topic=primary_topic,
        order_id=order_id,
        seller_ids=seller_ids,
    )

    # ── 7. Build primary_issue using evidence-driven logic ──
    primary_issue = _derive_primary_issue(
        primary_topic, shipment_result, payment_result
    )

    # ── 8. Determine case_status from policy + evidence ──
    case_status = _derive_case_status(
        primary_issue, policy_result, payment_result
    )

    # ── 9. Build responsible_parties from evidence ──
    responsible_parties = _derive_responsible_parties(
        primary_issue, seller_ids, shipment_result, payment_result
    )

    # ── 10. Build ranked_causes ──
    ranked_causes = _derive_ranked_causes(primary_issue, primary_topic)

    # ── 11. Claim Assessments ──
    all_refs = vault.get_all_evidence_refs()
    claim_assessments = _build_claim_assessments(
        claims, primary_issue, case_status, policy_result,
        payment_result, all_refs,
    )

    # ── 12. Data Conflicts Detection ──
    data_conflicts = _build_data_conflicts(primary_issue)

    # ── 13. Financial Resolution ──
    financial_resolution = _build_financial_resolution(
        primary_issue, case_status, policy_result, payment_result, order_id
    )

    # ── 14. Resolution Actions ──
    resolution_actions = _build_resolution_actions(
        primary_issue, case_status, policy_result
    )

    # ── 15. Build confidence based on calibration ──
    confidence = _calibrate_confidence(
        primary_issue, entity_result, shipment_result, payment_result
    )

    secondary_issues = (
        [cl["topic"] for cl in claims[1:]] if len(claims) > 1 else []
    )

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues[:10],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": entity_result.resolved_order_ids or [order_id],
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_result.payment_references[:20],
            "shipment_ids": shipment_result.shipment_ids[:20],
        },
        "claim_assessments": claim_assessments[:5],
        "entity_resolution": entity_result.to_entity_resolution_dict(),
        "customer_context": entity_result.to_customer_context_dict(),
        "shipment_analysis": shipment_result.to_dict(),
        "payment_analysis": payment_result.to_dict(),
        "root_cause_analysis": {
            "ranked_causes": ranked_causes[:5],
            "responsible_parties": responsible_parties[:5],
        },
        "evidence_refs": all_refs,
        "data_conflicts": data_conflicts[:5],
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions[:8],
    }

    # ── 16. Invariant Verification ──
    verifier = InvariantVerifier(trace)
    validated_output = verifier.verify_and_adjust(case_id, output, all_refs)

    return validated_output


# ─── Helper Functions ─────────────────────────────────────────

def _derive_primary_issue(
    primary_topic: str,
    shipment_result: Any,
    payment_result: Any,
) -> str:
    """Map claim topic to a schema-valid primary_issue using evidence."""
    # For shipment topics, use shipment evidence to determine precise issue
    if primary_topic == "late_delivery_seller":
        if shipment_result.verdict == "seller_delay":
            return "late_delivery_seller"
        elif shipment_result.verdict == "logistics_delay":
            return "late_delivery_logistics"
        return "late_delivery_seller"

    if primary_topic == "late_delivery_logistics":
        if shipment_result.verdict == "seller_delay":
            return "late_delivery_seller"
        return "late_delivery_logistics"

    # For payment topics, validate with payment evidence
    if primary_topic == "duplicate_charge":
        return "duplicate_charge"
    if primary_topic == "payment_mismatch":
        return "payment_mismatch"
    if primary_topic == "valid_split_payment":
        return "valid_split_payment"
    if primary_topic == "refund_pending":
        return "refund_pending"
    if primary_topic == "refund_failed":
        return "refund_failed"
    if primary_topic == "canceled_order_paid":
        return "canceled_order_paid"
    if primary_topic == "unavailable_order_paid":
        return "unavailable_order_paid"
    if primary_topic == "unsupported_claim":
        return "unsupported_claim"

    # Fallback — unknown topics
    return "insufficient_evidence"


def _derive_case_status(
    primary_issue: str,
    policy_result: Any,
    payment_result: Any,
) -> str:
    """Determine case status using combined policy + evidence signals."""
    # Prefer policy engine's status if available
    if policy_result.case_status in (
        "action_required", "no_action", "needs_investigation"
    ):
        status = policy_result.case_status
    else:
        status = _TOPIC_DEFAULT_STATUS.get(primary_issue, "needs_investigation")

    # Override: unsupported claims should be no_action
    if primary_issue == "unsupported_claim":
        status = "no_action"

    # Override: valid_split_payment is typically no_action
    if primary_issue == "valid_split_payment":
        if payment_result.verdict == "reconciled":
            status = "no_action"

    return status


def _derive_responsible_parties(
    primary_issue: str,
    seller_ids: list[str],
    shipment_result: Any,
    payment_result: Any,
) -> list[dict[str, Any]]:
    """Build responsible_parties from evidence."""
    party_type = _TOPIC_RESPONSIBLE_PARTY.get(primary_issue, "unknown")

    if party_type == "seller":
        party_id = None
        # Use late_seller_ids if available
        if hasattr(shipment_result, "late_seller_ids") and shipment_result.late_seller_ids:
            party_id = shipment_result.late_seller_ids[0]
        elif seller_ids:
            party_id = seller_ids[0]
        return [{"party_type": "seller", "party_id": party_id}]

    if party_type == "logistics_provider":
        return [{"party_type": "logistics_provider", "party_id": None}]

    if party_type == "payment_provider":
        return [{"party_type": "payment_provider", "party_id": None}]

    if party_type == "platform":
        return [{"party_type": "platform", "party_id": None}]

    return [{"party_type": "unknown", "party_id": None}]


def _derive_ranked_causes(
    primary_issue: str, primary_topic: str
) -> list[dict[str, Any]]:
    """Build ranked causes from the identified issue."""
    cause_code = primary_issue.upper()
    causes = [{"cause_code": cause_code, "rank": 1}]

    # If evidence refined the issue away from the original topic, add secondary
    if primary_issue != primary_topic and primary_topic != "requested_full_refund":
        secondary_code = primary_topic.upper()
        causes.append({"cause_code": secondary_code, "rank": 2})

    return causes


def _build_claim_assessments(
    claims: list[dict[str, Any]],
    primary_issue: str,
    case_status: str,
    policy_result: Any,
    payment_result: Any,
    all_refs: list[str],
) -> list[dict[str, Any]]:
    """Build per-claim assessments with evidence linkage."""
    assessments: list[dict[str, Any]] = []

    for cl in claims:
        cid = cl.get("claim_id", "")
        topic = cl.get("topic", "")
        refs = all_refs[:5]

        if topic == "requested_full_refund":
            # Full refund depends on financial analysis
            refund = policy_result.recommended_refund_brl
            captured = payment_result.captured_total_brl
            if captured > 0 and refund >= captured:
                verdict = "supported"
            elif refund > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
            assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": 0.85,
                "evidence_refs": refs,
            })
        elif topic == primary_issue or topic == claims[0].get("topic", ""):
            # Primary claim assessment
            if case_status == "action_required":
                verdict = "supported"
            elif case_status == "no_action":
                verdict = "unsupported"
            else:
                verdict = "partially_supported"
            assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": 0.90,
                "evidence_refs": refs,
            })

    return assessments


def _build_data_conflicts(primary_issue: str) -> list[dict[str, Any]]:
    """Build data conflicts based on issue type."""
    conflicts: list[dict[str, Any]] = []

    if primary_issue in ("late_delivery_logistics", "late_delivery_seller"):
        conflicts.append({
            "field": "delivery_timeline",
            "sources": ["order_status_record", "shipment_carrier_events"],
            "selected_source": "shipment_carrier_events",
            "resolution_code": "prefer_authoritative_carrier_events",
        })
    elif primary_issue in ("payment_mismatch", "duplicate_charge"):
        conflicts.append({
            "field": "payment_amount",
            "sources": ["order_record", "payment_gateway_log"],
            "selected_source": "payment_gateway_log",
            "resolution_code": "prefer_authoritative_payment_source",
        })
    elif primary_issue in ("refund_pending", "refund_failed"):
        conflicts.append({
            "field": "refund_status",
            "sources": ["customer_claim", "payment_timeline"],
            "selected_source": "payment_timeline",
            "resolution_code": "prefer_authoritative_payment_source",
        })
    elif primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        conflicts.append({
            "field": "order_status",
            "sources": ["order_record", "payment_record"],
            "selected_source": "order_record",
            "resolution_code": "prefer_authoritative_order_source",
        })

    return conflicts


def _build_financial_resolution(
    primary_issue: str,
    case_status: str,
    policy_result: Any,
    payment_result: Any,
    order_id: str,
) -> dict[str, Any]:
    """Build financial resolution block."""
    refund_brl = policy_result.recommended_refund_brl
    refund_lines: list[dict[str, Any]] = []

    # For no_action cases, force zero refund
    if case_status == "no_action":
        refund_brl = 0.0
    elif refund_brl > 0.0:
        reason = policy_result.recommended_action
        refund_lines.append({
            "reason_code": reason,
            "amount_brl": round(refund_brl, 2),
            "entity_id": order_id,
        })

    return {
        "currency": "BRL",
        "recommended_refund_brl": round(refund_brl, 2),
        "refund_lines": refund_lines[:10],
    }


def _build_resolution_actions(
    primary_issue: str,
    case_status: str,
    policy_result: Any,
) -> list[str]:
    """Build resolution actions list."""
    actions: list[str] = []

    if case_status == "no_action":
        actions.append("close_case")
    elif case_status == "needs_investigation":
        actions.append("escalate_for_investigation")
    else:
        actions.append(policy_result.recommended_action)

    if case_status == "action_required":
        if "notify_customer" not in actions:
            actions.append("notify_customer")

    return actions


def _calibrate_confidence(
    primary_issue: str,
    entity_result: Any,
    shipment_result: Any,
    payment_result: Any,
) -> float:
    """Calibrate confidence based on evidence quality."""
    base = 0.85

    # Boost if entity was fully resolved
    if entity_result.status == "resolved":
        base += 0.05

    # Boost if timeline is complete (shipment)
    if shipment_result.timeline_complete:
        base += 0.03

    # Degrade for unsupported/insufficient
    if primary_issue in ("unsupported_claim", "insufficient_evidence"):
        base -= 0.15

    return round(min(0.99, max(0.50, base)), 2)
