import pytest

from embry0.safety.error_codes import ErrorCode, category_for_code, classify_agent_error


def test_provider_codes_exist_with_a_category():
    assert ErrorCode.PROVIDER_RATE_LIMITED == "ERR_PROVIDER_RATE_LIMITED"
    assert ErrorCode.PROVIDER_QUOTA_EXHAUSTED == "ERR_PROVIDER_QUOTA_EXHAUSTED"
    assert category_for_code(ErrorCode.PROVIDER_RATE_LIMITED) is not None
    assert category_for_code(ErrorCode.PROVIDER_QUOTA_EXHAUSTED) is not None


@pytest.mark.parametrize(
    "text,code",
    [
        ("Error code: 429 - {'error': {'type': 'rate_limit_error'}}", ErrorCode.PROVIDER_RATE_LIMITED),
        ("You've hit your usage limit reached for this 5-hour window", ErrorCode.PROVIDER_QUOTA_EXHAUSTED),
        ("monthly quota exceeded", ErrorCode.PROVIDER_QUOTA_EXHAUSTED),
        ("rate limit exceeded", ErrorCode.PROVIDER_RATE_LIMITED),
        ("Error code: 401 - invalid x-api-key", ErrorCode.AUTH_REJECTED),
        ("Error code: 403 - forbidden", ErrorCode.AUTH_REJECTED),
        ("Command failed with exit code 1", ErrorCode.AGENT_PROCESS_FAILED),
    ],
)
def test_text_classification(text, code):
    assert classify_agent_error(text) == code
