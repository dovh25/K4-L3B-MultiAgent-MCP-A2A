from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .evidence_vault import EvidenceVault
from .trace import TraceWriter

HEX_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class EntityResolutionResult:
    status: str
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    confidence: float
    customer_unique_id: str | None
    related_order_ids: list[str]
    primary_order_id: str | None

    def to_entity_resolution_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "resolved_order_ids": self.resolved_order_ids[:20],
            "rejected_candidates": self.rejected_candidates[:20],
            "confidence": round(self.confidence, 4),
        }

    def to_customer_context_dict(self) -> dict[str, Any]:
        return {
            "customer_unique_id": self.customer_unique_id,
            "related_order_ids": self.related_order_ids[:20],
        }


class EntityResolver:
    """Specialist agent that filters fake candidates and validates real identities."""

    def __init__(self, vault: EvidenceVault, trace: TraceWriter) -> None:
        self.vault = vault
        self.trace = trace

    async def resolve(self, case: dict[str, Any]) -> EntityResolutionResult:
        case_id = case["case_id"]
        customer_request = case.get("customer_request", {})
        claimed_order_id = customer_request.get("claimed_order_id")
        candidate_ids = case.get("candidate_order_ids", [])
        customer_unique_id_hint = case.get("customer_unique_id_hint")

        # 1. Emit task assignment trace
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="entity-agent",
            attributes={"task": "entity_resolution"},
        )

        rejected: list[str] = []
        real_candidates: list[str] = []

        # 2. Filter out synthetically invalid candidates (e.g., candidate-xxx)
        for cand in candidate_ids:
            if not isinstance(cand, str):
                continue
            if cand.startswith("candidate-") or not HEX_ID_PATTERN.match(cand):
                rejected.append(cand)
            else:
                real_candidates.append(cand)

        related_order_ids: list[str] = []
        customer_unique_id: str | None = customer_unique_id_hint

        # 3. Consult authoritative customer history via MCP if hint provided
        if customer_unique_id_hint:
            try:
                evidence = await self.vault.call(
                    "get_customer_history",
                    actor="entity-agent",
                    customer_unique_id=customer_unique_id_hint,
                )
                orders = evidence.get("data", {}).get("orders", [])
                for o in orders:
                    oid = o.get("order_id")
                    if oid and oid not in related_order_ids:
                        related_order_ids.append(oid)
            except Exception:
                pass

        # 4. Resolve exact order ID based on evidence
        resolved_order_ids: list[str] = []
        confidence = 0.5
        status = "not_found"

        if claimed_order_id and claimed_order_id in related_order_ids:
            # Claimed order is confirmed by authoritative customer history
            resolved_order_ids = [claimed_order_id]
            for cand in real_candidates:
                if cand != claimed_order_id and cand not in rejected:
                    rejected.append(cand)
            status = "resolved"
            confidence = 1.0
        elif claimed_order_id and claimed_order_id in real_candidates:
            # Verify directly with get_order if not confirmed via customer history
            try:
                await self.vault.call(
                    "get_order",
                    actor="entity-agent",
                    order_id=claimed_order_id,
                )
                resolved_order_ids = [claimed_order_id]
                for cand in real_candidates:
                    if cand != claimed_order_id and cand not in rejected:
                        rejected.append(cand)
                status = "resolved"
                confidence = 0.95
            except Exception:
                rejected.append(claimed_order_id)
                status = "not_found"
                confidence = 0.75
        elif real_candidates:
            # Pick first candidate found in customer orders or verified
            matched = False
            for cand in real_candidates:
                if cand in related_order_ids:
                    resolved_order_ids = [cand]
                    matched = True
                    break
            if not matched:
                for cand in real_candidates:
                    try:
                        await self.vault.call(
                            "get_order",
                            actor="entity-agent",
                            order_id=cand,
                        )
                        resolved_order_ids = [cand]
                        matched = True
                        break
                    except Exception:
                        if cand not in rejected:
                            rejected.append(cand)
            if matched:
                status = "resolved"
                confidence = 0.90
                for cand in real_candidates:
                    if cand not in resolved_order_ids and cand not in rejected:
                        rejected.append(cand)
            else:
                status = "not_found"
                confidence = 0.75
        else:
            status = "not_found"
            confidence = 0.80

        # Deduplicate while preserving order
        resolved_order_ids = list(dict.fromkeys(resolved_order_ids))
        rejected = list(dict.fromkeys(rejected))
        related_order_ids = list(dict.fromkeys(related_order_ids))

        primary_order_id = resolved_order_ids[0] if resolved_order_ids else None

        # 5. Emit handoff trace event back to coordinator
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="entity-agent",
            target="coordinator",
            attributes={
                "status": status,
                "primary_order_id": primary_order_id or "none",
                "resolved_count": len(resolved_order_ids),
            },
        )

        return EntityResolutionResult(
            status=status,
            resolved_order_ids=resolved_order_ids,
            rejected_candidates=rejected,
            confidence=confidence,
            customer_unique_id=customer_unique_id,
            related_order_ids=related_order_ids,
            primary_order_id=primary_order_id,
        )
