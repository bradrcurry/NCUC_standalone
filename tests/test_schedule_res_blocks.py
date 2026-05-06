from pathlib import Path

from duke_rates.parse.schedule_parser import parse_schedule_text


def test_parse_progress_res_extracts_seasonal_blocks() -> None:
    text = Path(
        "data_api_nc2/raw/nc/progress/rate/"
        "residential-service-schedule-res-media-pdfs-for-your-home-rates-dep-nc-leaf-no-5.pdf.txt"
    ).read_text(encoding="utf-8")

    result = parse_schedule_text(
        document_id=110,
        title="Residential Service Schedule RES",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.schedule is not None
    assert len(result.schedule.energy_charges) >= 3

    october_rates = [
        charge for charge in result.schedule.energy_charges if charge.season == "October - April"
    ]
    may_rates = [
        charge for charge in result.schedule.energy_charges if charge.season == "May - September"
    ]

    assert october_rates[0].block_to == 800
    assert october_rates[1].block_from == 800
    assert may_rates[0].block_from == 0
