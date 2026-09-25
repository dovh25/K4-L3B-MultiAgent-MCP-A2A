from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence_vault import EvidenceVault
from .trace import TraceWriter


@dataclass(frozen=True)
class PolicyEvaluationResult:
    case_status: str
    recommended_action: str
    recommended_refund_brl: float
    refund_lines: list[dict[str, Any]]
    responsible_parties: list[dict[str, Any]]
    ranked_causes: list[dict[str, Any]]
    resolution_actions: list[str]

    def to_financial_resolution_dict(self) -> dict[str, Any]:
        return {
            "currency": "BRL",
            "recommended_refund_brl": round(self.recommended_refund_brl, 2),
            "refund_lines": self.refund_lines[:10],
        }

    def to_root_cause_dict(self) -> dict[str, Any]:
        return {
            "ranked_causes": self.ranked_causes[:5],
            "responsible_parties": self.responsible_parties[:5],
        }


class PolicyEngine:
    """Policy evaluator that matches claim topics against authoritative EC policy rules."""

    def __init__(self, vault: EvidenceVault, trace: TraceWriter) -> None:
        self.vault = vault
        self.trace = trace

    async def evaluate(
        self,
        case_id: str,
        policy_version: str,
        primary_topic: str,
        order_id: str,
        seller_ids: list[str] | None = None,
    ) -> PolicyEvaluationResult:
        # 1. Fetch authoritative policy rules via EvidenceVault
        policy_rules: dict[str, Any] = {}
        try:
            res = await self.vault.call(
                "get_policy",
                actor="policy-agent",
                policy_version=policy_version,
            )
            policy_rules = res.get("data", {}).get("rules", {})
        except Exception:
            pass

        rule = policy_rules.get(primary_topic, {})
        case_status = rule.get("case_status", "needs_investigation")
        recommended_action = rule.get("recommended_action", "investigate_case")
        recommended_refund_brl = float(rule.get("refund_brl", 0.0))

        # 2. Derive responsible parties
        parties = rule.get("responsible_parties", [])
        responsible_parties: list[dict[str, Any]] = []
        for p in parties:
            ptype = p.get("party_type", "unknown")
            pid = p.get("party_id")
            if ptype == "seller" and seller_ids and not pid:
                pid = seller_ids[0]
            responsible_parties.append({"party_type": ptype, "party_id": pid})

        if not responsible_parties:
            responsible_parties = [{"party_type": "platform", "party_id": None}]

        # 3. Derive ranked causes
        cause_code = primary_topic.upper()
        ranked_causes = [{"cause_code": cause_code, "rank": 1}]

        # 4. Construct refund lines
        refund_lines: list[dict[str, Any]] = []
        if recommended_refund_brl > 0.0:
            refund_lines.append(
                {
                    "reason_code": recommended_action,
                    "amount_brl": round(recommended_refund_brl, 2),
                    "entity_id": order_id,
                }
            )

        # 5. Build resolution actions list
        actions = [recommended_action]
        if case_status == "action_required" and "notify_customer" not in actions:
            actions.append("notify_customer")

        # 6. Emit observable trace event
        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            decision_code=recommended_action,
            attributes={
                "primary_issue": primary_topic,
                "refund_brl": recommended_refund_brl,
                "case_status": case_status,
            },
        )

        return PolicyEvaluationResult(
            case_status=case_status,
            recommended_action=recommended_action,
            recommended_refund_brl=recommended_refund_brl,
            refund_lines=refund_lines,
            responsible_parties=responsible_parties,
            ranked_causes=ranked_causes,
            resolution_actions=actions[:8],
        )
