from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RedlineSpanClue:
    page_number: int
    text: str
    context_text: str
    bbox: tuple[float, float, float, float]
    color_rgb: tuple[int, int, int] | None = None
    is_red_text: bool = False
    has_strikethrough_flag: bool = False
    overlapped_by_horizontal_line: bool = False
    annotation_types: list[str] = field(default_factory=list)


@dataclass
class RedlinePageParseResult:
    pdf_path: str
    page_number: int
    changed_span_count: int
    horizontal_line_count: int
    annotation_types: list[str] = field(default_factory=list)
    clues: list[RedlineSpanClue] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "page_number": self.page_number,
            "changed_span_count": self.changed_span_count,
            "horizontal_line_count": self.horizontal_line_count,
            "annotation_types": self.annotation_types,
            "clues": [asdict(clue) for clue in self.clues],
        }


def parse_redline_page(
    pdf_path: str,
    *,
    page_number: int,
    max_clues: int = 40,
) -> RedlinePageParseResult:
    """Extract changed-span clues from one PDF page using PyMuPDF.

    This is not a full semantic before/after diff. It is a geometry-aware clue
    extractor for tracked-change pages:
    - red-colored text spans
    - strikethrough span flags
    - thin horizontal vector lines overlapping text
    - markup annotations overlapping text
    - surrounding line context so a later pass can reconstruct old/new values
    """
    try:
        import fitz
    except ImportError as exc:
        raise ImportError("PyMuPDF (fitz) is required for redline page parsing") from exc

    source_pdf = Path(pdf_path)
    with fitz.open(source_pdf) as doc:
        page = doc[page_number - 1]
        raw = page.get_text("rawdict")
        horizontal_lines = _extract_horizontal_lines(page)
        annotation_regions = _extract_annotation_regions(page)

    clues: list[RedlineSpanClue] = []
    seen_keys: set[tuple[str, tuple[float, float, float, float]]] = set()

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            line_text = _line_text(line)
            for span in line.get("spans", []):
                text = _span_text(span).strip()
                if not text:
                    continue
                bbox = _bbox_tuple(span.get("bbox"))
                if bbox is None:
                    continue

                color_int = int(span.get("color") or 0)
                rgb = _rgb_tuple(color_int)
                is_red_text = _is_red(color_int)
                has_strikethrough_flag = _has_strikeout(span)
                overlapped_by_line = _bbox_overlaps_any_line(bbox, horizontal_lines)
                annotation_types = _annotation_hits(bbox, annotation_regions)

                if not any([is_red_text, has_strikethrough_flag, overlapped_by_line, annotation_types]):
                    continue
                if _should_skip_false_positive(
                    text=text,
                    is_red_text=is_red_text,
                    has_strikethrough_flag=has_strikethrough_flag,
                    overlapped_by_line=overlapped_by_line,
                    annotation_types=annotation_types,
                ):
                    continue

                key = (text, bbox)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                clues.append(
                    RedlineSpanClue(
                        page_number=page_number,
                        text=text,
                        context_text=line_text,
                        bbox=bbox,
                        color_rgb=rgb if is_red_text else None,
                        is_red_text=is_red_text,
                        has_strikethrough_flag=has_strikethrough_flag,
                        overlapped_by_horizontal_line=overlapped_by_line,
                        annotation_types=annotation_types,
                    )
                )

    clues.sort(
        key=lambda clue: (
            clue.page_number,
            clue.bbox[1],
            clue.bbox[0],
            clue.text,
        )
    )
    deduped_annotation_types = sorted(
        {annotation for clue in clues for annotation in clue.annotation_types}
    )
    return RedlinePageParseResult(
        pdf_path=str(source_pdf),
        page_number=page_number,
        changed_span_count=len(clues),
        horizontal_line_count=len(horizontal_lines),
        annotation_types=deduped_annotation_types,
        clues=clues[:max_clues],
    )


def _line_text(line: dict[str, Any]) -> str:
    parts: list[str] = []
    for span in line.get("spans", []):
        text = _span_text(span)
        if text:
            parts.append(text)
    return " ".join(part.strip() for part in parts if part.strip())


def _span_text(span: dict[str, Any]) -> str:
    chars = span.get("chars")
    if chars:
        return "".join(str(ch.get("c") or "") for ch in chars)
    return str(span.get("text") or "")


def _extract_horizontal_lines(page: Any) -> list[tuple[float, float, float, float]]:
    lines: list[tuple[float, float, float, float]] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is None:
            continue
        bbox = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
        height = abs(bbox[3] - bbox[1])
        width = abs(bbox[2] - bbox[0])
        if height <= 2.0 and width >= 20.0:
            lines.append(bbox)
    return lines


def _extract_annotation_regions(page: Any) -> list[tuple[str, tuple[float, float, float, float]]]:
    regions: list[tuple[str, tuple[float, float, float, float]]] = []
    annot_iter = page.annots()
    if annot_iter is None:
        return regions
    for annot in annot_iter:
        rect = annot.rect
        annot_type = annot.type[1] if annot.type else "unknown"
        regions.append(
            (
                str(annot_type),
                (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
            )
        )
    return regions


def _bbox_tuple(raw_bbox: Any) -> tuple[float, float, float, float] | None:
    if not raw_bbox or len(raw_bbox) != 4:
        return None
    return tuple(float(value) for value in raw_bbox)


def _rgb_tuple(color_int: int) -> tuple[int, int, int]:
    return (
        (color_int >> 16) & 0xFF,
        (color_int >> 8) & 0xFF,
        color_int & 0xFF,
    )


def _is_red(color_int: int) -> bool:
    r, g, b = _rgb_tuple(color_int)
    return r > 150 and g < 100 and b < 100


def _bbox_overlaps_any_line(
    bbox: tuple[float, float, float, float],
    lines: list[tuple[float, float, float, float]],
) -> bool:
    x0, y0, x1, y1 = bbox
    for lx0, ly0, lx1, ly1 in lines:
        horizontal_overlap = min(x1, lx1) - max(x0, lx0)
        if horizontal_overlap <= 3.0:
            continue
        line_y = (ly0 + ly1) / 2.0
        if y0 <= line_y <= y1:
            return True
    return False


def _annotation_hits(
    bbox: tuple[float, float, float, float],
    annotation_regions: list[tuple[str, tuple[float, float, float, float]]],
) -> list[str]:
    hits: list[str] = []
    x0, y0, x1, y1 = bbox
    for annot_type, (ax0, ay0, ax1, ay1) in annotation_regions:
        horizontal_overlap = min(x1, ax1) - max(x0, ax0)
        vertical_overlap = min(y1, ay1) - max(y0, ay0)
        if horizontal_overlap > 0 and vertical_overlap > 0:
            hits.append(annot_type)
    return sorted(set(hits))


def _has_strikeout(span: dict[str, Any]) -> bool:
    span_char_flags = span.get("char_flags")
    if span_char_flags is not None:
        return bool(int(span_char_flags) & 1)
    for ch in span.get("chars", []):
        if bool(int(ch.get("char_flags") or 0) & 1):
            return True
    return False


def _should_skip_false_positive(
    *,
    text: str,
    is_red_text: bool,
    has_strikethrough_flag: bool,
    overlapped_by_line: bool,
    annotation_types: list[str],
) -> bool:
    normalized = text.strip()
    if normalized in {"o", "O", "-", "*", "•"} and not is_red_text and not annotation_types:
        return True
    if (
        overlapped_by_line
        and not is_red_text
        and not has_strikethrough_flag
        and not annotation_types
        and normalized.isupper()
        and len(normalized) >= 5
    ):
        return True
    return False


__all__ = [
    "RedlinePageParseResult",
    "RedlineSpanClue",
    "parse_redline_page",
]
