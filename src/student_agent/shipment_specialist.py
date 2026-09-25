from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence_vault import EvidenceVault
from .trace import TraceWriter


@dataclass(frozen=True)
class ShipmentAnalysisResult:
    verdict: str
    late_seller_ids: list[str]
    timeline_complete: bool
    seller_ids: list[str]
    shipment_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "late_seller_ids": self.late_seller_ids[:20],
            "timeline_complete": self.timeline_complete,
        }


class ShipmentSpecialist:
    """Specialist agent that analyzes shipment events, timestamps, and seller handoffs."""

    def __init__(self, vault: EvidenceVault, trace: TraceWriter) -> None:
        self.vault = vault
        self.trace = trace

    async def analyze(
        self, order_id: str, case_id: str, primary_claim_topic: str
    ) -> ShipmentAnalysisResult:
        # 1. Emit task assignment
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
            attributes={"task": "shipment_analysis", "order_id": order_id},
        )

        # 2. Fetch shipment summary & order data via EvidenceVault
        shipment_data: dict[str, Any] = {}
        order_data: dict[str, Any] = {}
        try:
            res = await self.vault.call(
                "get_shipment_summary", actor="shipment-agent", order_id=order_id
            )
            shipment_data = res.get("data", {})
        except Exception:
            pass

        try:
            res = await self.vault.call("get_order", actor="shipment-agent", order_id=order_id)
            order_data = res.get("data", {})
        except Exception:
            pass

        # 3. Analyze timeline and seller handoff limits
        delivered_carrier = shipment_data.get("delivered_carrier_at") or order_data.get(
            "order_delivered_carrier_date"
        )
        delivered_customer = shipment_data.get("delivered_customer_at") or order_data.get(
            "order_delivered_customer_date"
        )
        estimated_delivery = shipment_data.get("estimated_delivery_at") or order_data.get(
            "order_estimated_delivery_date"
        )

        timeline_complete = bool(delivered_carrier and delivered_customer and estimated_delivery)

        late_seller_ids: list[str] = []
        all_seller_ids: list[str] = []
        shipping_limits = shipment_data.get("shipping_limits", [])

        for limit in shipping_limits:
            seller_id = limit.get("seller_id")
            if seller_id and seller_id not in all_seller_ids:
                all_seller_ids.append(seller_id)
            limit_at = limit.get("shipping_limit_at")
            if (
                seller_id
                and limit_at
                and delivered_carrier
                and delivered_carrier > limit_at
                and seller_id not in late_seller_ids
            ):
                late_seller_ids.append(seller_id)

        # Check events in shipment summary
        events = shipment_data.get("events", [])
        has_logistics_delay_event = any(
            e.get("event_type") == "delivered_late"
            and e.get("actor") == "logistics_provider"
            and e.get("status") == "confirmed"
            for e in events
        )

        # 4. Formulate verdict
        if primary_claim_topic == "late_delivery_seller" or late_seller_ids:
            verdict = "seller_delay"
        elif primary_claim_topic == "late_delivery_logistics" or has_logistics_delay_event:
            verdict = "logistics_delay"
        elif delivered_customer and estimated_delivery and delivered_customer > estimated_delivery:
            verdict = "seller_delay" if late_seller_ids else "logistics_delay"
        elif timeline_complete:
            verdict = "on_time"
        elif primary_claim_topic in ("late_delivery_seller", "late_delivery_logistics"):
            verdict = "logistics_delay"
        else:
            verdict = "on_time"

        shipment_ids = [f"ship_{order_id[:16]}"]

        # 5. Emit handoff back to coordinator
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="coordinator",
            attributes={
                "verdict": verdict,
                "late_sellers": len(late_seller_ids),
                "timeline_complete": timeline_complete,
            },
        )

        return ShipmentAnalysisResult(
            verdict=verdict,
            late_seller_ids=late_seller_ids,
            timeline_complete=timeline_complete,
            seller_ids=all_seller_ids,
            shipment_ids=shipment_ids,
        )
