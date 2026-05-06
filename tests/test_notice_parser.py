from duke_rates.parse.notice_parser import parse_notice_text


def test_notice_parser_extracts_dockets_and_links() -> None:
    text = """
    NOTICE TO CUSTOMERS OF CHANGE IN RATES
    DOCKET NO. E-2, SUB 1341
    DOCKET NO. E-2, SUB 1344
    DOCKET NO. E-2, SUB 1345
    NOTICE IS HEREBY GIVEN that the Commission entered an Order in Docket No. E-2, Sub 1341.
    These changes are effective as of December 1, 2024.
    The notice also concerns CPRE Program Cost Recovery Rider
    and Joint Agency Asset Cost Recovery Rider.
    Residential, Small General Service, Medium General Service,
    Large General Service, and Lighting customer classes.
    """

    result = parse_notice_text(
        document_id=1,
        title="NC Annual Riders Notice – Fuel, REPS, CPRE, DSM/EE, JAAR",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.notice is not None
    assert "E-2, Sub 1341" in result.notice.docket_numbers
    assert "E-2, Sub 1344" in result.notice.docket_numbers
    assert {"CPRE", "JAA", "BA", "REPS", "DSM/EE"} <= set(result.notice.related_rider_codes)
    assert {"RES", "SGS", "MGS", "LGS", "LIGHTING"} <= set(result.notice.related_schedule_codes)
