import pytest

from partner_agent.runtime.recovery import recovery_message


@pytest.mark.parametrize(
    "code",
    [
        "provider_invalid_design",
        "provider_invalid_rag_json",
        "provider_unauthorized_citation",
        "provider_request_timeout",
        "provider_request_failed",
        "provider_daily_cap_exceeded",
        "provider_request_cap_exceeded",
    ],
)
def test_expected_provider_failures_have_plain_text_guidance(code):
    message = recovery_message(code)
    assert message and len(message) < 500 and code not in message


@pytest.mark.parametrize(
    "code", ["source_context_changed", "access_revoked", "database_broken", "unrecognized_error"]
)
def test_security_and_unknown_infrastructure_failures_are_not_masked(code):
    assert recovery_message(code) is None
