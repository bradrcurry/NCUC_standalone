from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from duke_rates.db.repository import Repository
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.models.document import StoredDocument
from duke_rates.parse.nc_carolinas import parse_nc_carolinas_leaf_file
from duke_rates.parse.nc_progress import parse_nc_progress_leaf_file
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.rider_summary import parse_rider_summary

LEAF_PATH_RE = re.compile(r"leaf-no-(\d+)", re.I)


def audit_local_raw_docs(
    repository: Repository,
    *,
    state: str = "NC",
    company: str,
    include_current: bool = True,
    include_historical: bool = True,
) -> dict[str, object]:
    company_key = company.lower()
    state_key = state.lower()
    roots: list[tuple[str, Path]] = []
    if include_current:
        roots.append(("current", Path("data") / "raw" / state_key / company_key))
    if include_historical:
        roots.append(("historical", Path("data") / "historical" / "raw" / state_key / company_key))

    current_docs = repository.list_documents(state=state.upper(), company=company_key)
    by_leaf = _build_leaf_document_map(current_docs)

    report_rows: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for root_label, root_path in roots:
        for category_dir in ("rate", "rider"):
            scan_dir = root_path / category_dir
            if not scan_dir.is_dir():
                continue
            for pdf_path in sorted(scan_dir.glob("*.pdf")):
                row = _audit_single_pdf(
                    pdf_path=pdf_path,
                    root_label=root_label,
                    category=category_dir,
                    company=company_key,
                    leaf_docs=by_leaf,
                )
                report_rows.append(row)
                if row.get("status") != "parsed":
                    errors.append(
                        {
                            "path": str(pdf_path),
                            "status": row.get("status"),
                            "error": row.get("error"),
                        }
                    )

    summary = _summarize(report_rows)
    return {
        "state": state.upper(),
        "company": company_key,
        "include_current": include_current,
        "include_historical": include_historical,
        "summary": summary,
        "rows": report_rows,
        "errors": errors,
    }


def write_local_raw_audit(report: dict[str, object], output_json: Path, output_csv: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    rows = report.get("rows", [])
    headers = [
        "root",
        "category",
        "path",
        "leaf_no",
        "effective_start",
        "revision_label",
        "family_key",
        "parsed_schedule_code",
        "parsed_rider_code",
        "summary_rate_classes",
        "summary_res_total_cents",
        "charge_count",
        "rider_link_count",
        "status",
        "error",
    ]
    lines = [",".join(headers)]
    for row in rows:
        values = []
        for header in headers:
            value = row.get(header, "")
            text = "" if value is None else str(value)
            if any(ch in text for ch in [",", "\"", "\n"]):
                text = "\"" + text.replace("\"", "\"\"") + "\""
            values.append(text)
        lines.append(",".join(values))
    output_csv.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _audit_single_pdf(
    *,
    pdf_path: Path,
    root_label: str,
    category: str,
    company: str,
    leaf_docs: dict[str, StoredDocument],
) -> dict[str, object]:
    text = extract_pdf_text(pdf_path)
    metadata = extract_historical_metadata(text)
    leaf_no = _leaf_no_for_path(pdf_path, metadata.get("leaf_no"))
    matching_doc = leaf_docs.get(leaf_no or "")
    family_key = _family_key_from_document(matching_doc, company=company, leaf_no=leaf_no)

    row: dict[str, object] = {
        "root": root_label,
        "category": category,
        "path": str(pdf_path),
        "leaf_no": leaf_no,
        "effective_start": metadata.get("effective_start"),
        "revision_label": metadata.get("revision_label"),
        "family_key": family_key,
        "parsed_schedule_code": None,
        "parsed_rider_code": None,
        "summary_rate_classes": None,
        "summary_res_total_cents": None,
        "charge_count": 0,
        "rider_link_count": 0,
        "status": "parsed",
        "error": None,
    }

    try:
        if category == "rider" and leaf_no in {"600", "99"}:
            summary = parse_rider_summary(text, source_pdf=str(pdf_path), leaf_no=leaf_no)
            row["summary_rate_classes"] = ",".join(block.rate_class for block in summary.rate_classes)
            residential = next(
                (
                    block
                    for block in summary.rate_classes
                    if "RESIDENTIAL" in block.rate_class.upper()
                    or "RS" in {code.upper() for code in block.applicable_schedules}
                    or "RES" in {code.upper() for code in block.applicable_schedules}
                ),
                None,
            )
            if residential is not None:
                row["summary_res_total_cents"] = residential.total_cents_per_kwh
        elif category == "rider":
            parsed = parse_rider_text(
                document_id=0,
                title=pdf_path.stem,
                state="NC",
                company="DEP" if company == "progress" else "DEC",
                text=text,
                raw_text_path=pdf_path.with_suffix(pdf_path.suffix + ".txt"),
            )
            if parsed.rider:
                row["parsed_rider_code"] = parsed.rider.code
                row["effective_start"] = parsed.rider.effective_date or row["effective_start"]
                row["charge_count"] = len(parsed.rider.charge_components)
        else:
            version, charges, riders = _parse_rate_pdf(company, pdf_path, family_key)
            row["effective_start"] = version.effective_start or row["effective_start"]
            row["revision_label"] = version.revision_label or row["revision_label"]
            row["parsed_schedule_code"] = _schedule_code_from_family_key(family_key)
            row["charge_count"] = len(charges)
            row["rider_link_count"] = len(riders)
    except Exception as exc:
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"

    return row


def _parse_rate_pdf(company: str, pdf_path: Path, family_key: str):
    if company == "progress":
        return parse_nc_progress_leaf_file(
            pdf_path,
            version_id=0,
            family_key=family_key,
            document_id=None,
        )
    return parse_nc_carolinas_leaf_file(
        pdf_path,
        version_id=0,
        family_key=family_key,
        document_id=None,
    )


def _build_leaf_document_map(documents: list[StoredDocument]) -> dict[str, StoredDocument]:
    by_leaf: dict[str, StoredDocument] = {}
    for doc in documents:
        leaf_no = _leaf_from_url(str(doc.document_url))
        if leaf_no and leaf_no not in by_leaf:
            by_leaf[leaf_no] = doc
    return by_leaf


def _leaf_no_for_path(path: Path, metadata_leaf: str | None) -> str | None:
    if metadata_leaf:
        match = re.search(r"(\d+)", metadata_leaf)
        if match:
            return match.group(1)
    match = LEAF_PATH_RE.search(path.name)
    return match.group(1) if match else None


def _leaf_from_url(url: str) -> str | None:
    match = LEAF_PATH_RE.search(url)
    return match.group(1) if match else None


def _family_key_from_document(
    document: StoredDocument | None,
    *,
    company: str,
    leaf_no: str | None,
) -> str:
    if document is not None:
        return urlparse(str(document.document_url)).path.lower()
    if company == "progress":
        return f"nc-progress-leaf-{leaf_no or 'unknown'}"
    return f"nc-carolinas-leaf-{leaf_no or 'unknown'}"


def _schedule_code_from_family_key(family_key: str) -> str | None:
    token = family_key.split("schedule-", maxsplit=1)
    if len(token) == 2:
        return token[1].split(".", maxsplit=1)[0].upper()
    return None


def _summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    rate_dates = sorted(
        {
            str(row["effective_start"])
            for row in rows
            if row.get("category") == "rate" and row.get("effective_start")
        }
    )
    rider_dates = sorted(
        {
            str(row["effective_start"])
            for row in rows
            if row.get("category") == "rider" and row.get("effective_start")
        }
    )
    summary_dates = sorted(
        {
            str(row["effective_start"])
            for row in rows
            if row.get("summary_res_total_cents") is not None and row.get("effective_start")
        }
    )
    return {
        "total_files": len(rows),
        "parsed_ok": sum(1 for row in rows if row.get("status") == "parsed"),
        "parse_errors": sum(1 for row in rows if row.get("status") != "parsed"),
        "rate_files": sum(1 for row in rows if row.get("category") == "rate"),
        "rider_files": sum(1 for row in rows if row.get("category") == "rider"),
        "distinct_rate_effective_dates": rate_dates,
        "distinct_rider_effective_dates": rider_dates,
        "distinct_summary_effective_dates": summary_dates,
    }
