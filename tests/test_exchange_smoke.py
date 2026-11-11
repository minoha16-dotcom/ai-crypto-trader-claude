import pytest

from scripts.check_exchanges import is_region_restriction


@pytest.mark.parametrize(
    "message",
    [
        "ExchangeNotAvailable: HTTP 451 restricted location",
        "403 Forbidden: The Amazon CloudFront distribution is configured to block access from your country",
    ],
)
def test_exchange_smoke_classifies_explicit_geo_blocks_as_skips(message):
    assert is_region_restriction(message)


@pytest.mark.parametrize(
    "message",
    [
        "RequestTimeout: network timeout",
        "RateLimitExceeded: too many requests",
        "403 Forbidden: invalid request",
    ],
)
def test_exchange_smoke_does_not_hide_non_geographic_failures(message):
    assert not is_region_restriction(message)
