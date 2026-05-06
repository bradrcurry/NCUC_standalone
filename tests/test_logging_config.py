import logging

from duke_rates.logging_config import _RedactSecretsFilter


def test_redact_secrets_filter_redacts_api_key_query_values() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "HTTP Request: GET "
            "https://api.openei.org/utility_rates?version=8&api_key=supersecret123&limit=25 "
            '"HTTP/1.1 200 OK"'
        ),
        args=(),
        exc_info=None,
    )

    allowed = _RedactSecretsFilter().filter(record)

    assert allowed is True
    assert "supersecret123" not in record.msg
    assert "api_key=[REDACTED]" in record.msg
