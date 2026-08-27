import pytest

from linkedin_profile_api.urls import InvalidProfileURL, canonical_url, public_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/in/williamhgates/", "williamhgates"),
        ("https://www.linkedin.com/in/williamhgates", "williamhgates"),
        # Query junk LinkedIn itself appends when you share a profile.
        ("https://www.linkedin.com/in/foo?originalSubdomain=in", "foo"),
        ("https://www.linkedin.com/in/foo/?trk=public_profile_browsemap", "foo"),
        # Sub-path tabs, i.e. someone copied the URL from a details view.
        ("https://www.linkedin.com/in/foo/details/experience/", "foo"),
        ("https://www.linkedin.com/in/foo/recent-activity/all/", "foo"),
        # Locale hosts and the mobile-web prefix.
        ("https://in.linkedin.com/in/foo", "foo"),
        ("https://uk.linkedin.com/in/foo", "foo"),
        ("https://www.linkedin.com/mwlite/in/foo", "foo"),
        # Missing scheme, which urlsplit otherwise reads as a bare path.
        ("www.linkedin.com/in/foo-bar-123", "foo-bar-123"),
        ("linkedin.com/in/foo", "foo"),
        # Percent-encoded unicode vanity name, decoded exactly once.
        ("https://www.linkedin.com/in/%E5%B1%B1%E7%94%B0-%E5%A4%AA%E9%83%8E-123", "山田-太郎-123"),
        # Legacy pre-2016 profile URL: best effort at the name segment.
        ("https://www.linkedin.com/pub/jane-doe/1/2a/3b4", "jane-doe"),
        # A bare slug, as a convenience.
        ("williamhgates", "williamhgates"),
        ("  https://www.linkedin.com/in/foo/  ", "foo"),
        ("HTTPS://WWW.LINKEDIN.COM/IN/Foo", "Foo"),
    ],
)
def test_accepts(raw, expected):
    assert public_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://www.linkedin.com/company/linkedin/",
        "https://www.linkedin.com/school/mit/",
        "https://www.linkedin.com/posts/foo_activity-123",
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/jobs/view/123",
        "https://example.com/in/foo",
        "https://linkedin.com.evil.example/in/foo",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/",
    ],
)
def test_rejects(raw):
    with pytest.raises(InvalidProfileURL):
        public_id(raw)


def test_rejection_says_why():
    with pytest.raises(InvalidProfileURL, match="company page"):
        public_id("https://www.linkedin.com/company/linkedin/")


def test_canonical_url_roundtrips():
    assert canonical_url(public_id("linkedin.com/in/foo?x=1")) == (
        "https://www.linkedin.com/in/foo/"
    )
