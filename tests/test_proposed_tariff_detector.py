from __future__ import annotations

from duke_rates.document_intelligence.proposed_tariff_detector import (
    detect_blocks_from_sections,
    detect_exhibit_context,
    extract_rate_fields,
    find_schedule_name,
    is_current_baseline,
)


def test_detect_exhibit_context_reads_myrp_rate_year_anchors() -> None:
    context = detect_exhibit_context(
        "Application Exhibit B_1 Duke Energy Carolinas MYRP Rate Year 1"
    )

    assert context is not None
    assert context.exhibit_key == "B_1"
    assert context.rate_year_context == "MYRP Rate Year 1"


def test_detect_exhibit_context_excludes_current_exhibit_a() -> None:
    assert (
        detect_exhibit_context(
            "Application Exhibit A Current North Carolina Schedules, Riders, "
            "and Other Tariffs Proposed for Change"
        )
        is None
    )
    assert is_current_baseline("Exhibit A Current Schedules")


def test_find_schedule_name_reads_target_headers() -> None:
    assert (
        find_schedule_name("Residential Service Schedule RES basic charge")
        == "RESIDENTIAL SERVICE SCHEDULE RES"
    )
    assert (
        find_schedule_name(
            "Large General Service (Real Time Pricing) Schedule LGS-RTP"
        )
        == "LARGE GENERAL SERVICE (REAL TIME PRICING) SCHEDULE LGS-RTP"
    )
    assert find_schedule_name("The schedule was delayed by order") is None
    assert find_schedule_name("The schedule drivers are discussed") is None
    assert find_schedule_name("This schedule shall apply monthly") is None
    assert find_schedule_name("This schedule nor any rider applies") is None
    assert find_schedule_name("Service is available under Schedule LGS-TOU") == "SCHEDULE LGS-TOU"
    assert find_schedule_name("RIDER BA\nBilling Adjustment") == "RIDER BA"


def test_detect_exhibit_context_reads_dec_rate_year_tariff_heading() -> None:
    context = detect_exhibit_context(
        "Rate Year 2 North Carolina Tariffs Proposed for Change"
    )

    assert context is not None
    assert context.exhibit_key == "B_2"
    assert context.rate_year_context == "Rate Year 2 North Carolina Tariffs Proposed for Change"


def test_extract_rate_fields_keeps_ambiguous_charge_lines_as_evidence() -> None:
    fields = extract_rate_fields(
        """
        Basic Customer Charge: $19.75 per month
        Kilowatt-Hour Charge Summer On-Peak 14.25 cents per kWh
        Off-Peak Discount 2.10 cents per kWh
        """
    )

    assert fields["basic_customer_charge"] == "19.75"
    assert fields["volumetric_energy_charge_lines"]
    assert fields["time_of_use_lines"]


def test_detect_blocks_carries_target_exhibit_context_forward() -> None:
    sections = [
        {
            "id": 1,
            "source_pdf": "e-7-rate-case-application.pdf",
            "section_index": 0,
            "start_page": 10,
            "end_page": 10,
            "section_type": "procedural",
            "schedule_codes_json": "[]",
        },
        {
            "id": 2,
            "source_pdf": "e-7-rate-case-application.pdf",
            "section_index": 1,
            "start_page": 11,
            "end_page": 12,
            "section_type": "rate_schedule",
            "schedule_codes_json": '["RES"]',
        },
        {
            "id": 3,
            "source_pdf": "e-7-rate-case-application.pdf",
            "section_index": 2,
            "start_page": 13,
            "end_page": 13,
            "section_type": "rate_schedule",
            "schedule_codes_json": '["SGS"]',
        },
        {
            "id": 4,
            "source_pdf": "e-7-rate-case-application.pdf",
            "section_index": 3,
            "start_page": 14,
            "end_page": 14,
            "section_type": "rate_schedule",
            "schedule_codes_json": '["MGS"]',
        },
    ]
    text_by_id = {
        1: "PBR Application Application Exhibit B_1 MYRP Rate Year 1 proposed schedules",
        2: """
           Residential Service Schedule RES
           Basic Customer Charge: $18.25 per month
           Kilowatt-Hour Charge 12.00 cents per kWh
        """,
        3: "Application Exhibit A Current North Carolina Schedules Small General Service Schedule SGS",
        4: "Medium General Service Schedule MGS Basic Customer Charge: $30.00",
    }

    blocks = detect_blocks_from_sections(sections, lambda row: text_by_id[row["id"]])

    assert len(blocks) == 1
    assert blocks[0].section_id == 2
    assert blocks[0].exhibit_key == "B_1"
    assert blocks[0].schedule_name == "RESIDENTIAL SERVICE SCHEDULE RES"
    assert blocks[0].basic_customer_charge == "18.25"


def test_detect_blocks_requires_target_application_by_default() -> None:
    sections = [
        {
            "id": 1,
            "source_pdf": "rider-exhibit.pdf",
            "section_index": 0,
            "start_page": 1,
            "end_page": 1,
            "section_type": "rate_schedule",
            "schedule_codes_json": '["RES"]',
        }
    ]
    text = "Exhibit B Residential Service Schedule RES Basic Customer Charge: $10.00"

    assert detect_blocks_from_sections(sections, lambda row: text) == []
    assert detect_blocks_from_sections(
        sections,
        lambda row: text,
        require_target_document=False,
    )


def test_detect_blocks_extracts_starred_new_rider_catalog_entries() -> None:
    sections = [
        {
            "id": 1,
            "source_pdf": "e-2-rate-case-application.pdf",
            "section_index": 178,
            "start_page": 178,
            "end_page": 178,
            "section_type": "unknown",
            "schedule_codes_json": "[]",
        }
    ]
    text = """
        Application Exhibit B
        Index of Tariffs
        Proposed North Carolina Schedules, Riders, and Other Tariffs
        RETAIL RIDERS
        Leaf 600
        Summary of Rider Adjustments
        *Leaf 614
        Pensions Costs Rider PC
        *Leaf 615
        Production Tax Credits Rider PTC
        Leaf 640
        Residential Service Energy Conservation Discount Rider RECD
        *New Riders
        PBR Application
    """

    blocks = detect_blocks_from_sections(sections, lambda row: text)

    assert [block.schedule_name for block in blocks] == [
        "RIDER PC PENSIONS COSTS",
        "RIDER PTC PRODUCTION TAX CREDITS",
    ]
    assert all(block.section_type == "rider_catalog" for block in blocks)
