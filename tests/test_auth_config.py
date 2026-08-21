"""Which credentials a deployment needs, and what happens when they're wrong."""

from __future__ import annotations

import pytest

from jtr import auth, config
from jtr.dialect import CLOUD, SERVER

SERVER_URL = "https://tracker.example.com/jira"
CLOUD_URL = "https://acme.atlassian.net"


def configure(base_url, **values):
    config.set_value(config.KEY_BASE_URL, base_url)
    for key, value in values.items():
        config.set_value(key, value)
    return config.load()


# -- Dialect resolution from stored config -----------------------------


def test_cloud_url_is_no_longer_rejected():
    """The headline change: a Cloud base URL is a supported configuration."""
    d = auth.check_supported(CLOUD_URL)
    assert d.deployment == CLOUD


def test_deployment_override_is_persisted_and_honoured():
    cfg = configure("https://jira.acme.com", **{config.KEY_DEPLOYMENT: "cloud"})
    assert cfg.deployment == "cloud"
    assert auth.dialect_for(cfg).is_cloud


def test_blank_deployment_falls_back_to_detection():
    cfg = configure(CLOUD_URL, **{config.KEY_DEPLOYMENT: ""})
    assert auth.dialect_for(cfg).deployment == CLOUD


def test_v3_on_server_is_an_unsupported_deployment_error():
    cfg = configure(SERVER_URL, **{config.KEY_API_VERSION: "3"})
    with pytest.raises(auth.UnsupportedDeployment, match="no REST API v3"):
        auth.dialect_for(cfg)


# -- Credential completeness -------------------------------------------


def test_cloud_token_without_email_is_not_usable():
    """Basic auth needs both halves; a token alone can't authenticate."""
    configure(CLOUD_URL, **{config.KEY_PAT: "tok"})
    with pytest.raises(auth.SessionExpired, match="account email"):
        auth.load_credentials()


def test_cloud_with_email_and_token_is_usable():
    configure(CLOUD_URL, **{config.KEY_PAT: "tok", config.KEY_EMAIL: "me@acme.com"})
    creds = auth.load_credentials()
    assert creds.usable
    assert creds.dialect.is_cloud
    assert creds.email == "me@acme.com"


def test_server_pat_alone_is_usable():
    configure(SERVER_URL, **{config.KEY_PAT: "pat"})
    creds = auth.load_credentials()
    assert creds.usable
    assert creds.dialect.deployment == SERVER


def test_no_credentials_at_all_is_reported():
    configure(SERVER_URL)
    with pytest.raises(auth.SessionExpired, match="No credentials"):
        auth.load_credentials()


def test_cookies_are_not_sent_to_cloud(monkeypatch):
    """Cookies mean nothing to Cloud; carrying them would be noise."""
    monkeypatch.setattr(
        auth,
        "_read_cookies",
        lambda: auth.CookieState(base_url=CLOUD_URL, cookies=[{"name": "a", "value": "b"}]),
    )
    configure(CLOUD_URL, **{config.KEY_PAT: "tok", config.KEY_EMAIL: "me@acme.com"})
    assert auth.load_credentials().cookies == []


# -- Method selection ---------------------------------------------------


def test_methods_offered_per_deployment():
    assert auth.methods_for(auth.check_supported(CLOUD_URL)) == ("token",)
    assert auth.methods_for(auth.check_supported(SERVER_URL)) == ("sso", "pat")


def test_saved_method_that_cannot_work_is_discarded():
    """An sso config whose base URL moved to Cloud must not replay sso."""
    cfg = configure(
        CLOUD_URL,
        **{
            config.KEY_AUTH_METHOD: "sso",
            config.KEY_PAT: "tok",
            config.KEY_EMAIL: "me@acme.com",
        },
    )
    assert auth.resolve_method(cfg) == "token"


def test_saved_server_method_is_kept():
    cfg = configure(SERVER_URL, **{config.KEY_AUTH_METHOD: "pat", config.KEY_PAT: "p"})
    assert auth.resolve_method(cfg) == "pat"


def test_sso_refuses_on_cloud_before_opening_a_browser(monkeypatch):
    """A browser login would succeed and still authenticate nothing."""
    def explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("browser must not be launched for Cloud")

    monkeypatch.setattr(auth.Browser, "launch", explode)
    with pytest.raises(auth.UnsupportedDeployment, match="jtr auth token"):
        auth.sso_login(CLOUD_URL)


# -- Storing credentials ------------------------------------------------


def test_set_api_token_persists_both_halves():
    creds = auth.set_api_token(CLOUD_URL, "me@acme.com", "tok")
    assert creds.dialect.is_cloud
    cfg = config.load()
    assert cfg.email == "me@acme.com"
    assert cfg.pat == "tok"


def test_logout_clears_the_email_too():
    auth.set_api_token(CLOUD_URL, "me@acme.com", "tok")
    auth.clear(pat=True, cookies=False)
    cfg = config.load()
    assert not cfg.pat
    assert not cfg.email


def test_status_reports_deployment():
    configure(CLOUD_URL, **{config.KEY_PAT: "tok", config.KEY_EMAIL: "me@acme.com"})
    s = auth.status()
    assert s["deployment"] == CLOUD
    assert s["email"] == "me@acme.com"
    assert s["pat"] == "set"
