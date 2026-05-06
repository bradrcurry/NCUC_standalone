from __future__ import annotations

from duke_rates.config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def summarize_tariff(self, text: str) -> str:
        if not self.settings.openai_api_key:
            raise RuntimeError("No AI backend configured. Set DUKE_RATES_OPENAI_API_KEY.")
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Install duke-rates[ai] to enable OpenAI integration.") from exc

        client = OpenAI(api_key=self.settings.openai_api_key)
        response = client.responses.create(
            model=self.settings.openai_model,
            input=text[:12000],
        )
        return response.output_text
