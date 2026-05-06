from __future__ import annotations

from duke_rates.billing.calculators import UsageInput
from duke_rates.billing.engine import BillingEngine
from duke_rates.db.repository import Repository


def list_documents(
    repository: Repository, *, state: str | None = None, company: str | None = None
) -> list[dict]:
    return [
        doc.model_dump(mode="json")
        for doc in repository.list_documents(state=state, company=company)
    ]


def get_document(repository: Repository, document_id: int) -> dict | None:
    document = repository.get_document(document_id)
    return document.model_dump(mode="json") if document else None


def estimate_bill(parse_result_json: dict, usage: UsageInput) -> dict:
    from duke_rates.models.parse_result import DocumentParseResult

    engine = BillingEngine()
    parse_result = DocumentParseResult.model_validate(parse_result_json)
    if not parse_result.schedule:
        raise ValueError("Parsed document does not contain a schedule.")
    return engine.estimate(parse_result.schedule, usage).model_dump(mode="json")
