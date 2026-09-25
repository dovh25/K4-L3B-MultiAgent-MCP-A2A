from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from student_agent.contracts import Contracts
from student_agent.entity_resolver import EntityResolver
from student_agent.evidence_vault import EvidenceVault
from student_agent.trace import TraceWriter


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


@pytest.fixture
def trace_writer(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


def test_entity_resolver_filters_fake_candidates(
    trace_writer: TraceWriter,
) -> None:
    async def _run() -> None:
        gateway = MagicMock()
        gateway.call = AsyncMock(
            return_value={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_01234567890123456789012345",
                "result_hash": "sha256:" + "a" * 64,
                "domain": "customer",
                "data": {
                    "customer_unique_id": "cust-1",
                    "orders": [{"order_id": "11111111111111111111111111111111"}],
                },
            }
        )

        vault = EvidenceVault(gateway, trace_writer, "L3B_CASE_001")
        resolver = EntityResolver(vault, trace_writer)

        case = {
            "case_id": "L3B_CASE_001",
            "customer_request": {
                "claimed_order_id": "11111111111111111111111111111111",
            },
            "candidate_order_ids": [
                "11111111111111111111111111111111",
                "candidate-999",
            ],
            "customer_unique_id_hint": "cust-1",
        }

        result = await resolver.resolve(case)
        assert result.status == "resolved"
        assert result.resolved_order_ids == ["11111111111111111111111111111111"]
        assert "candidate-999" in result.rejected_candidates
        assert result.confidence == 1.0
        assert result.primary_order_id == "11111111111111111111111111111111"

        # Verify vault recorded the evidence_ref
        assert vault.get_all_evidence_refs() == ["ev_01234567890123456789012345"]

    asyncio.run(_run())
