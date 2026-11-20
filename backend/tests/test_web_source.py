import pytest

from partner_agent.documents.web_source import normalize_source_url


@pytest.mark.parametrize("url", [
    "http://example.org/a", "javascript:alert(1)", "https://127.0.0.1/a",
    "https://0x7f.1/a", "https://[::1]/a", "https://service.internal/a",
    "https://example.org:8443/a", "https://trusted.org@hostile.org/a",
    "https://trusted.org\\@hostile.org/a", "https://example.org/\na",
    "https:///example.org", "https://%65xample.org/a",
])
def test_rejects_unsafe_or_ambiguous_snapshot_addresses(url):
    with pytest.raises(ValueError, match="invalid_source_url"):
        normalize_source_url(url)


def test_normalizes_an_https_snapshot_address():
    assert normalize_source_url("https://EXAMPLE.org:443/курсы?course=204#details") == (
        "https://example.org/%D0%BA%D1%83%D1%80%D1%81%D1%8B?course=204#details"
    )
