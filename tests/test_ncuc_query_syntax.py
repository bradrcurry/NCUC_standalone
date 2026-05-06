from duke_rates.historical.ncuc.query_syntax import classify_pattern_type, sanitize_ncuc_query


def test_sanitize_ncuc_query_strips_risky_special_syntax_when_not_safe() -> None:
    result = sanitize_ncuc_query(
        '“Duke Energy Progress” (tariff OR rider) rate:schedule',
        safe_pattern_types={"single_term", "two_term"},
    )
    assert result == "Duke Energy Progress tariff rider rate schedule"


def test_sanitize_ncuc_query_preserves_safe_boolean_and_suffix_wildcard() -> None:
    result = sanitize_ncuc_query(
        'Duke AND tariff*',
        safe_pattern_types={"single_term", "two_term", "boolean_and", "suffix_wildcard"},
    )
    assert result == "Duke AND tariff*"
    assert classify_pattern_type(result) == "boolean_and"
