import pytest

from scripts.vike_shadow_client import publish


def test_remote_http_is_rejected_before_network_call():
    with pytest.raises(ValueError, match="HTTPS"):
        publish("http://example.com", {}, "user", "pass")


def test_missing_event_fields_are_rejected_before_network_call():
    with pytest.raises(ValueError, match="missing required fields"):
        publish("https://example.com", {}, "user", "pass")
