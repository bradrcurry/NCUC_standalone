from __future__ import annotations

from typing import Any

from duke_rates.document_intelligence.models import (
    DocumentRepresentation,
    LayoutBlock,
    NormalizationBackend,
    NormalizationMetrics,
    NormalizationWarning,
    PageRepresentation,
)


class DocumentRepresentationBuilder:
    """Build a normalized in-memory representation from existing pipeline artifacts."""

    def build_historical_document(
        self,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None = None,
    ) -> DocumentRepresentation:
        pages: list[PageRepresentation] = []
        if page_artifacts:
            for page in sorted(page_artifacts, key=lambda item: int(item.get("page_number") or 0)):
                text = str(page.get("text_content") or "")
                pages.append(
                    PageRepresentation(
                        page_number=int(page.get("page_number") or 0),
                        text=text,
                        source="page_artifact",
                        blocks=self._build_blocks(text, int(page.get("page_number") or 0)),
                        metadata=dict(page.get("metadata") or {}),
                    )
                )
        else:
            page_number = int(doc.get("start_page") or 1)
            pages.append(
                PageRepresentation(
                    page_number=page_number,
                    text=raw_text,
                    source="bounded_text",
                    blocks=self._build_blocks(raw_text, page_number),
                    metadata={},
                )
            )

        return DocumentRepresentation(
            source_pdf=str(doc.get("local_path") or ""),
            file_hash=doc.get("content_hash"),
            historical_document_id=int(doc["id"]) if doc.get("id") is not None else None,
            company=doc.get("company"),
            state=doc.get("state"),
            family_key=doc.get("family_key"),
            title=doc.get("title"),
            page_start=doc.get("start_page"),
            page_end=doc.get("end_page"),
            raw_text=raw_text,
            normalizer_backend=NormalizationBackend.PAGE_ARTIFACT if page_artifacts else NormalizationBackend.NATIVE_PDF,
            pages=pages,
            normalization_metrics=NormalizationMetrics(
                backend=NormalizationBackend.PAGE_ARTIFACT if page_artifacts else NormalizationBackend.NATIVE_PDF,
                page_count=len(pages),
                text_char_count=len(raw_text),
            ),
            document_metadata={
                "artifact_source": "page_artifact" if page_artifacts else "bounded_text",
                "effective_start": doc.get("effective_start"),
                "revision_label": doc.get("revision_label"),
                "supersedes_label": doc.get("supersedes_label"),
                "leaf_no": doc.get("leaf_no"),
                "docket_number": doc.get("docket_number"),
                "acquisition_method": doc.get("acquisition_method"),
                "discovery_doc_quality_tier": doc.get("discovery_doc_quality_tier"),
            },
        )

    def build_normalized_document(
        self,
        doc: dict[str, Any],
        *,
        pages: list[PageRepresentation],
        raw_text: str | None = None,
        markdown_text: str | None = None,
        backend: NormalizationBackend,
        metrics: NormalizationMetrics | None = None,
        warnings: list[NormalizationWarning] | None = None,
        document_metadata: dict[str, Any] | None = None,
    ) -> DocumentRepresentation:
        effective_raw_text = raw_text if raw_text is not None else "\n\n".join(page.text for page in pages)
        return DocumentRepresentation(
            source_pdf=str(doc.get("local_path") or ""),
            file_hash=doc.get("content_hash"),
            historical_document_id=int(doc["id"]) if doc.get("id") is not None else None,
            company=doc.get("company"),
            state=doc.get("state"),
            family_key=doc.get("family_key"),
            title=doc.get("title"),
            page_start=doc.get("start_page"),
            page_end=doc.get("end_page"),
            raw_text=effective_raw_text,
            markdown_text=markdown_text,
            normalizer_backend=backend,
            pages=pages,
            warnings=warnings or [],
            normalization_metrics=metrics or NormalizationMetrics(
                backend=backend,
                page_count=len(pages),
                text_char_count=len(effective_raw_text),
            ),
            document_metadata={
                "artifact_source": backend.value,
                "effective_start": doc.get("effective_start"),
                "revision_label": doc.get("revision_label"),
                "supersedes_label": doc.get("supersedes_label"),
                "leaf_no": doc.get("leaf_no"),
                "docket_number": doc.get("docket_number"),
                "acquisition_method": doc.get("acquisition_method"),
                "discovery_doc_quality_tier": doc.get("discovery_doc_quality_tier"),
                **(document_metadata or {}),
            },
        )

    @staticmethod
    def _build_blocks(raw_text: str, page_number: int) -> list[LayoutBlock]:
        blocks: list[LayoutBlock] = []
        for paragraph in (chunk.strip() for chunk in raw_text.split("\n\n")):
            if not paragraph:
                continue
            lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
            block_type = "header" if lines and len(lines[0]) < 80 and lines[0].isupper() else "paragraph"
            blocks.append(
                LayoutBlock(
                    block_type=block_type,
                    text=paragraph,
                    page_number=page_number,
                    confidence=0.7,
                )
            )
        return blocks
