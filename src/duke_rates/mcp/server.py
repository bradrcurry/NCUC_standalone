from __future__ import annotations

from duke_rates.billing.calculators import UsageInput
from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.mcp import tools


def serve(settings: Settings) -> None:
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Install duke-rates[mcp] to run the MCP server.") from exc

    repository = Repository(settings.database_path)
    server = FastMCP("duke-rates")

    @server.tool()
    def list_downloaded_documents(
        state: str | None = None, company: str | None = None
    ) -> list[dict]:
        return tools.list_documents(repository, state=state, company=company)

    @server.tool()
    def fetch_document(document_id: int) -> dict | None:
        return tools.get_document(repository, document_id)

    @server.tool()
    def estimate_simple_bill(
        parse_result: dict, monthly_kwh: float, peak_kw: float | None = None
    ) -> dict:
        usage = UsageInput(monthly_kwh=monthly_kwh, peak_kw=peak_kw)
        return tools.estimate_bill(parse_result, usage)

    server.run()
