"""Native structural controls; archived-page scores live in bench/redline_native.py."""

from __future__ import annotations

import fitz
import pytest

from duke_rates.parse.redline_detector import (
    PageRedlineEvidence,
    evaluate_redline_page,
    summarize_page_evidence,
)


def _page(text="Energy charge 0.123 cents/kWh", color=(0, 0, 0)):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, color=color)
    return doc, page


def test_black_vector_strike_on_rate_preserves_exact_geometry():
    doc, page = _page()
    value = page.search_for("0.123")[0]
    y = value.y0 + value.height * 0.55
    page.draw_line((value.x0, y), (value.x1, y), color=(0, 0, 0))
    evidence = evaluate_redline_page(page, 17)
    assert evidence.classification == "confirmed_material_redline"
    observation = next(o for o in evidence.observations if o["method"] == "vector_strikeout")
    assert observation["page"] == 17 and observation["text"] == "Energy charge 0.123 cents/kWh"
    assert observation["mark_bbox"] == pytest.approx([value.x0, y, value.x1, y])
    assert evidence.region_struck(tuple(value))
    doc.close()


@pytest.mark.parametrize("decoration", ["table_border", "heading", "negative_sign", "box"])
def test_black_decoration_is_not_material_redline(decoration):
    doc, page = _page("Energy charge -0.123 cents/kWh")
    if decoration == "table_border":
        page.draw_line((50, 104), (550, 104))
    elif decoration == "heading":
        page.draw_line((72, 102), (230, 102))
    elif decoration == "negative_sign":
        page.draw_line((146, 96), (149, 96))
    else:
        page.draw_rect(fitz.Rect(50, 70, 550, 120))
    evidence = evaluate_redline_page(page, 1)
    assert evidence.verdict != "redline"
    doc.close()


def test_unrelated_logo_cannot_corroborate_heading_insertion():
    doc, page = _page("DEFERRED COSTS")
    span = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
    x0, y0, x1, y1 = span["bbox"]
    y = y0 + (y1 - y0) * 0.95
    page.draw_line((x0, y), (x1, y), color=(0, 0, 1))
    page.insert_text((72, 200), "Company logo", color=(1, 0, 0))
    evidence = evaluate_redline_page(page, 1)
    assert evidence.verdict == "suspect"
    doc.close()


def test_blue_email_underline_is_ambiguous_not_tariff_insertion():
    doc, page = _page("hayes.finley@duke-energy.com", color=(0, 0, 1))
    bbox = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]["bbox"]
    y = bbox[1] + (bbox[3] - bbox[1]) * 0.95
    page.draw_line((bbox[0], y), (bbox[2], y), color=(0, 0, 1))
    evidence = evaluate_redline_page(page, 1)
    assert evidence.verdict == "suspect"
    assert any(o.get("decorative_link") for o in evidence.observations)
    doc.close()


def test_multiline_strike_annotation_preserves_individual_quads():
    doc, page = _page("Old charge 0.123")
    page.insert_text((72, 150), "Unrelated middle line")
    page.insert_text((72, 200), "Old charge 0.456")
    rects = page.search_for("Old charge")
    page.add_strikeout_annot(rects)
    evidence = evaluate_redline_page(page, 4)
    assert evidence.strike_annotations == 1
    assert len(evidence.strike_regions) >= 2
    assert all(r[3] - r[1] < 20 for r in evidence.strike_regions)
    assert not evidence.region_struck(tuple(page.search_for("Unrelated")[0]))
    doc.close()


@pytest.mark.parametrize(
    "annotation", ["add_underline_annot", "add_highlight_annot", "add_squiggly_annot"]
)
def test_annotations_without_deletion_are_ambiguous(annotation):
    doc, page = _page()
    getattr(page, annotation)(page.search_for("Energy charge"))
    evidence = evaluate_redline_page(page, 1)
    assert evidence.verdict == "suspect"
    assert evidence.classification == "ambiguous_markup"
    doc.close()


def test_duplicate_strike_evidence_does_not_inflate_coverage():
    ev = PageRedlineEvidence(page=1, verdict="redline", strike_regions=[(0, 0, 2, 10)] * 4)
    assert not ev.region_struck((0, 0, 10, 10), 0.5)


def test_classification_is_machine_evidence_and_page_scoped():
    clean = PageRedlineEvidence(page=2, verdict="clean")
    struck = PageRedlineEvidence(page=3, verdict="redline")
    insufficient = PageRedlineEvidence(page=4, verdict="no_text_layer")
    summary = summarize_page_evidence([clean, struck, insufficient])
    assert summary["redline_pages"] == [3]
    assert summary["insufficient_pages"] == [4]
    assert summary["evaluated_pages"] == [2, 3, 4]
    assert clean.classification == "no_redline_evidence_detected"
    assert clean.signals()["human_verified"] is False


def test_image_page_with_searchable_overlay_is_insufficient():
    doc, page = _page()
    pix = page.get_pixmap()
    image_doc = fitz.open()
    raster = image_doc.new_page()
    raster.insert_image(raster.rect, pixmap=pix)
    raster.insert_text((72, 100), "OCR Energy charge 0.123", render_mode=3)
    evidence = evaluate_redline_page(raster, 9)
    assert evidence.verdict == "no_text_layer"
    assert evidence.classification == "insufficient_evidence"
    image_doc.close()
    doc.close()


def test_synthetic_colored_heading_is_not_sufficient_for_rate_insertion():
    doc, page = _page("MONTHLY RATE", color=(1, 0, 0))
    bbox = page.search_for("MONTHLY RATE")[0]
    page.draw_line((bbox.x0, bbox.y1 - 1), (bbox.x1, bbox.y1 - 1), color=(1, 0, 0))
    assert evaluate_redline_page(page, 1).verdict == "suspect"
    doc.close()


def test_label_strike_does_not_claim_deletion_of_nearby_rate():
    doc, page = _page("Old label 0.123")
    rect = page.search_for("Old label")[0]
    y = rect.y0 + rect.height * 0.55
    page.draw_line((rect.x0, y), (rect.x1, y), color=(1, 0, 0))
    evidence = evaluate_redline_page(page, 1)
    assert evidence.verdict == "redline"
    assert evidence.classification == "likely_redline"
    assert not any(o.get("material_value") for o in evidence.observations)
    doc.close()
