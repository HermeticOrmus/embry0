from embry0.safety.error_codes import ErrorCode, category_for_code


def test_credential_codes_exist_and_are_config_category() -> None:
    assert ErrorCode.CREDENTIAL_MISSING == "ERR_CREDENTIAL_MISSING"
    assert ErrorCode.CREDENTIAL_UNAVAILABLE == "ERR_CREDENTIAL_UNAVAILABLE"
    assert category_for_code(ErrorCode.CREDENTIAL_MISSING) == category_for_code(ErrorCode.AUTH_REJECTED)
    assert category_for_code(ErrorCode.CREDENTIAL_UNAVAILABLE) == category_for_code(ErrorCode.AUTH_REJECTED)
