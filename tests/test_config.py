"""Cookie handling. Small surface, but a mistake here is a 403 that looks like
a parsing bug, so the two supply routes both get pinned down.
"""

import pytest

from linkedin_profile_api import config

FULL_HEADER = 'bcookie="v=2&abc"; JSESSIONID="ajax:99"; li_at=AQEDxyz; lidc="b=OB1:s=O"'


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for name in ("LI_COOKIE", "LI_AT", "LI_JSESSIONID"):
        monkeypatch.delenv(name, raising=False)


def test_two_cookie_mode(monkeypatch):
    monkeypatch.setenv("LI_AT", "AQEDxyz")
    monkeypatch.setenv("LI_JSESSIONID", '"ajax:99"')
    assert config.cookies() == {"li_at": "AQEDxyz", "JSESSIONID": '"ajax:99"'}
    assert config.csrf_token() == "ajax:99"
    assert config.session_configured()


def test_unquoted_jsessionid_is_accepted(monkeypatch):
    """The browser value carries quotes; pasting without them must still work,
    because csrf-token needs it bare while the cookie needs it quoted."""
    monkeypatch.setenv("LI_AT", "x")
    monkeypatch.setenv("LI_JSESSIONID", "ajax:99")
    assert config.csrf_token() == "ajax:99"
    assert config.cookies()["JSESSIONID"] == '"ajax:99"'


def test_full_cookie_header_mode(monkeypatch):
    monkeypatch.setenv("LI_COOKIE", FULL_HEADER)
    jar = config.cookies()
    assert jar["li_at"] == "AQEDxyz"
    assert jar["bcookie"] == '"v=2&abc"'  # quotes preserved — LinkedIn sent them
    assert jar["lidc"] == '"b=OB1:s=O"'
    assert config.csrf_token() == "ajax:99"
    assert config.session_configured()


def test_csrf_cannot_disagree_with_the_cookie(monkeypatch):
    """LI_COOKIE wins over a stale LI_JSESSIONID, so the pair can never mismatch."""
    monkeypatch.setenv("LI_COOKIE", FULL_HEADER)
    monkeypatch.setenv("LI_JSESSIONID", '"ajax:stale"')
    assert config.csrf_token() == "ajax:99"


def test_cookies_never_reach_the_headers(monkeypatch):
    """Cookies belong in the jar; a static header would clobber lidc rotation."""
    monkeypatch.setenv("LI_COOKIE", FULL_HEADER)
    assert "cookie" not in config.headers()
    assert config.headers()["csrf-token"] == "ajax:99"


def test_unconfigured_is_reported_not_guessed():
    assert not config.session_configured()


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("a=1; b=2", {"a": "1", "b": "2"}),
        ("  a=1 ;  b=2  ", {"a": "1", "b": "2"}),
        ("a=1; malformed; b=2", {"a": "1", "b": "2"}),  # skip, don't crash
        ("a=v=2&x", {"a": "v=2&x"}),  # value containing "=" survives
        ("", {}),
    ],
)
def test_cookie_header_parsing(header, expected):
    assert config.parse_cookie_header(header) == expected
