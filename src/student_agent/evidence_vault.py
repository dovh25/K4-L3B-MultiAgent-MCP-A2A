from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class EvidenceVault:
    """Safe wrapper around EvidenceGateway that manages caching, audit evidence tracking,

    and trace emission for MCP calls per case.
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter, case_id: str) -> None:
        self.gateway = gateway
        self.trace = trace
        self.case_id = case_id
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self._evidence_refs: list[str] = []
        self._evidences: dict[str, dict[str, Any]] = {}
        self._domain_evidences: dict[str, list[dict[str, Any]]] = {}

    async def call(
        self, tool_name: str, *, actor: str = "coordinator", **arguments: str
    ) -> dict[str, Any]:
        """Call an MCP tool with per-case caching, trace logging, and evidence tracking."""
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]

        evidence = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
        self._cache[cache_key] = evidence

        ref = evidence.get("evidence_ref")
        if ref and ref not in self._evidence_refs:
            self._evidence_refs.append(ref)
            self._evidences[ref] = evidence
            domain = evidence.get("domain", "unknown")
            self._domain_evidences.setdefault(domain, []).append(evidence)

            # Emit observable trace event linking tool result and evidence to the actor
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[ref],
                attributes={"domain": domain},
            )

        return evidence

    def get_all_evidence_refs(self) -> list[str]:
        """Return unique list of all evidence refs collected for this case (capped at 30)."""
        seen: set[str] = set()
        unique_refs: list[str] = []
        for ref in self._evidence_refs:
            if ref not in seen:
                seen.add(ref)
                unique_refs.append(ref)
        return unique_refs[:30]

    def get_evidences_by_domain(self, domain: str) -> list[dict[str, Any]]:
        """Return all evidence responses collected for a given domain."""
        return self._domain_evidences.get(domain, [])

    def get_evidence(self, ref: str) -> dict[str, Any] | None:
        """Lookup evidence object by ref."""
        return self._evidences.get(ref)
