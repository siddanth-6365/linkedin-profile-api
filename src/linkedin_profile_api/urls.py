"""Profile URL -> public identifier (the `/in/<slug>` part).

The only untrusted input the service takes, so it gets a real parser and real
tests rather than a regex that works on the happy path. The slug is returned
*decoded*; the HTTP layer re-encodes it once when building the query. Decoding
twice is how unicode vanity URLs break.
"""

from urllib.parse import unquote, urlsplit

# Profile URLs live under /in/ (current) or /pub/ (legacy, pre-2016).
PROFILE_SEGMENTS = ("in", "pub")

# Segments that mean "a LinkedIn URL, but not a person" — worth naming in the
# error so the caller knows it was rejected on purpose.
NOT_A_PROFILE = {
    "company": "a company page",
    "school": "a school page",
    "posts": "a post",
    "feed": "a feed page",
    "jobs": "a job posting",
    "groups": "a group",
    "showcase": "a showcase page",
    "learning": "a LinkedIn Learning page",
    "newsletters": "a newsletter",
    "pulse": "an article",
}


class InvalidProfileURL(ValueError):
    """The input is not a LinkedIn member profile URL."""


def public_id(raw: str) -> str:
    """Extract the public identifier from a LinkedIn profile URL.

    Handles trailing slashes, query strings (`?originalSubdomain=in`), sub-path
    tabs (`/details/experience/`), locale hosts (`in.linkedin.com`), the mobile
    web prefix (`/mwlite/in/`), and percent-encoded unicode vanity names. A bare
    slug is accepted as a convenience.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidProfileURL("No URL supplied.")

    # A bare slug, e.g. "williamhgates". No path and no host to speak of.
    if "/" not in text and "." not in text and " " not in text:
        return _validate(unquote(text))

    # urlsplit puts everything in `path` when the scheme is missing, which loses
    # the host and with it the linkedin.com check.
    if "://" not in text:
        text = "https://" + text.lstrip("/")

    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        raise InvalidProfileURL(
            f"Not a linkedin.com URL (host was {host!r}). "
            "Expected something like https://www.linkedin.com/in/<name>."
        )

    segments = [s for s in parts.path.split("/") if s]
    for i, segment in enumerate(segments):
        lowered = segment.lower()
        if lowered in PROFILE_SEGMENTS:
            if i + 1 >= len(segments):
                raise InvalidProfileURL(f"URL has /{lowered}/ but no profile name after it.")
            # ponytail: for legacy /pub/<name>/1/2a/3b4 the trailing segments are
            # an obfuscated member id, and <name> is usually — not always — the
            # public identifier. Best effort; LinkedIn redirects these anyway.
            return _validate(unquote(segments[i + 1]))
        if lowered in NOT_A_PROFILE:
            raise InvalidProfileURL(
                f"That is {NOT_A_PROFILE[lowered]}, not a member profile. "
                "Expected a /in/<name> URL."
            )

    raise InvalidProfileURL(
        "Could not find a profile name in the URL. "
        "Expected something like https://www.linkedin.com/in/<name>."
    )


def _validate(slug: str) -> str:
    slug = slug.strip().strip("/")
    if not slug:
        raise InvalidProfileURL("Profile name is empty.")
    if "/" in slug or any(c.isspace() for c in slug):
        raise InvalidProfileURL(f"Profile name {slug!r} contains an illegal character.")
    if len(slug) > 200:
        raise InvalidProfileURL("Profile name is implausibly long.")
    return slug


def canonical_url(slug: str) -> str:
    """The tidied URL we echo back, so callers can see what we resolved to."""
    return f"https://www.linkedin.com/in/{slug}/"
