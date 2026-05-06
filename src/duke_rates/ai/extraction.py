from __future__ import annotations

from duke_rates.ai.llm_client import LLMClient
from duke_rates.ai.prompting import build_tariff_extraction_prompt


def run_ai_assisted_extraction(client: LLMClient, text: str) -> str:
    prompt = build_tariff_extraction_prompt(text)
    return client.summarize_tariff(prompt)
