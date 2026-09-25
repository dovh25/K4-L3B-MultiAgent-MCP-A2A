from __future__ import annotations

import os

import httpx2
from dotenv import load_dotenv

from .trace import TraceWriter


class LLMReasoningAgent:
    """Multi-Agent specialist powered by a cloud LLM (<= 10B parameters).

    Performs semantic claim reasoning and conflict adjudication with graceful fallback.
    """

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace
        load_dotenv()
        self.api_key = os.getenv("LLM_API_KEY", "").strip()
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
        self.model = os.getenv("LLM_MODEL", "allam-2-7b").strip()
        self.enabled = bool(self.api_key and not self.api_key.endswith("replace_me"))

    async def analyze_claim_intent(
        self, case_id: str, customer_message: str, primary_topic: str
    ) -> dict[str, str]:
        """Perform semantic reasoning on the customer message using the LLM."""
        if not self.enabled:
            return {"status": "disabled", "summary": "Rule-based analysis"}

        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="llm-reasoner-agent",
            attributes={"task": "semantic_claim_reasoning", "model": self.model},
        )

        prompt = (
            f"You are an AI claim specialist. The customer message is: '{customer_message}'. "
            f"The identified issue is '{primary_topic}'. "
            "In 1 concise sentence (under 30 words), state the core customer demand."
        )

        summary = f"Customer claims {primary_topic}"
        status = "completed"

        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with httpx2.AsyncClient(headers=headers, timeout=10.0) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": "You are a concise e-commerce agent."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 60,
                        "temperature": 0.2,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        summary = choices[0].get("message", {}).get("content", "").strip()
                else:
                    status = "degraded_fallback"
        except Exception:
            status = "fallback"

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="llm-reasoner-agent",
            target="coordinator",
            attributes={"status": status, "model": self.model},
        )

        return {"status": status, "summary": summary}
