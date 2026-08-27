"""Settings, plus the LinkedIn strings most likely to rot.

Every volatile value is env-overridable on purpose. When LinkedIn renames a
resource or bumps a decoration version, the fix should be a redeployed variable
rather than a code change — `identity/profiles/{id}/profileView` answering
410 Gone (it used to be *the* endpoint) is what that failure looks like.
"""

import base64
import json
import os
import secrets
from pathlib import Path

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"


def load_env(path: str | Path = ".env") -> None:
    """Read a .env into os.environ without taking a dependency for it.

    Existing environment variables win, so Render's dashboard values are never
    shadowed by a stray local file.
    """
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")  # cookies contain "="; split once
        value = value.strip()
        # Strip quotes only when they wrap the whole value: a pasted Cookie
        # header can legitimately end in `"` (e.g. lidc="b=..."), and a blind
        # strip would silently corrupt it.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# --- session -----------------------------------------------------------------

def li_at() -> str:
    return os.getenv("LI_AT", "").strip()


def li_jsessionid() -> str:
    return os.getenv("LI_JSESSIONID", "").strip()


def api_key() -> str:
    return os.getenv("API_KEY", "").strip()


def li_cookie() -> str:
    """A whole browser Cookie header, pasted verbatim (optional).

    Preferred over LI_AT/LI_JSESSIONID when available. LinkedIn weighs how
    consistent a request looks against the browser the session came from, and a
    request carrying only two of the browser's dozen cookies does not look
    consistent. Copying the entire Cookie header from a real request in DevTools
    reproduces it exactly — including bcookie/bscookie (browser identity) and
    lidc (datacenter routing), whose absence causes the self-redirects we saw.
    """
    return os.getenv("LI_COOKIE", "").strip()


def parse_cookie_header(header: str) -> dict[str, str]:
    """`a=1; b="2"` -> {"a": "1", "b": '"2"'}. Values keep their quotes."""
    jar = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            jar[name.strip()] = value.strip()
    return jar


def cookies() -> dict[str, str]:
    """The cookie jar to send. LI_COOKIE wins; otherwise the two-cookie minimum."""
    if li_cookie():
        jar = parse_cookie_header(li_cookie())
        jar.setdefault("li_at", li_at())
        return {name: value for name, value in jar.items() if value}
    return {"li_at": li_at(), "JSESSIONID": f'"{csrf_token()}"'}


def csrf_token() -> str:
    """LinkedIn's CSRF token is JSESSIONID without its quotes.

    Read out of LI_COOKIE when that is how the session was supplied, so the two
    can never disagree — a mismatch is exactly `403 CSRF check failed`.
    """
    if li_cookie():
        found = parse_cookie_header(li_cookie()).get("JSESSIONID", "")
        if found:
            return found.strip('"')
    return li_jsessionid().strip('"')


def session_configured() -> bool:
    if li_cookie():
        return bool(csrf_token() and cookies().get("li_at"))
    return bool(li_at() and li_jsessionid())


# --- volatile LinkedIn surface ------------------------------------------------

PROFILE_DECORATION = _env(
    "LI_PROFILE_DECORATION",
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93",
)

# Section resources under identity/dash, called as ?q=viewee&profileUrn={urn}.
# All ten verified live (HTTP 401 with a valid csrf-token and no li_at, where a
# nonexistent resource answers 404). The primary profile call truncates these
# lists the same way the web UI does, so each is fetched in full separately.
#
# ponytail: profilePositions (flat) not profilePositionGroups (grouped by
# company) — flat needs no ungrouping, and the normalizer reads dateRange for
# ordering anyway. Swap if grouped roles at one employer ever need preserving.
SECTIONS: dict[str, str] = {
    "experience": "profilePositions",
    "education": "profileEducations",
    "skills": "profileSkills",
    "certifications": "profileCertifications",
    "languages": "profileLanguages",
    "projects": "profileProjects",
    "honors": "profileHonors",
    "volunteer": "profileVolunteerExperiences",
    "publications": "profilePublications",
}

# Must stay consistent with sec-ch-ua below: a UA claiming one Chrome version
# while the client hints claim another is a worse signal than sending neither.
USER_AGENT = _env(
    "LI_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
)

LI_LANG = _env("LI_LANG", "en_US")

# Voyager's own client identifies itself on every call. Sending nothing where the
# real front end sends these marks a request as not-a-browser for free, so they
# are mirrored here. Defaults were read off a live voyager-web request; bump
# LI_CLIENT_VERSION when it drifts (it moves every week or two).
LI_CLIENT_VERSION = _env("LI_CLIENT_VERSION", "1.13.46243")
# Keep this consistent with wherever the session was created: a cookie minted in
# Asia/Calcutta paired with a UTC client is a free inconsistency to hand over.
LI_TIMEZONE = _env("LI_TIMEZONE", "Asia/Calcutta")
LI_TIMEZONE_OFFSET = _float("LI_TIMEZONE_OFFSET", 5.5)


def _track_header() -> str:
    return json.dumps(
        {
            "clientVersion": LI_CLIENT_VERSION,
            "mpVersion": LI_CLIENT_VERSION,
            "osName": "web",
            "timezoneOffset": LI_TIMEZONE_OFFSET,
            "timezone": LI_TIMEZONE,
            "deviceFormFactor": "DESKTOP",
            "mpName": "voyager-web",
            "displayDensity": 2,
            "displayWidth": 1920,
            "displayHeight": 1080,
        },
        separators=(",", ":"),
    )


def page_instance() -> str:
    """A per-request page instance urn, as the web client sends.

    The trackingId is a fresh base64 16-byte value each time; a constant one
    across thousands of requests would itself be the tell.
    """
    token = base64.b64encode(secrets.token_bytes(16)).decode()
    return f"urn:li:page:d_flagship3_profile_view_base;{token}"


def headers() -> dict[str, str]:
    """Voyager rejects a request missing any of these.

    Cookies are deliberately absent — they live in the client's jar (see
    `cookies()`) so that LinkedIn's own set-cookie updates apply to later calls
    instead of being overwritten by a static header.

    `accept: application/vnd.linkedin.normalized+json+2.1` is the important one:
    it makes LinkedIn answer with a flat `included` entity table instead of a
    nested tree, which is what the normalizer is built around.
    """
    return {
        "user-agent": USER_AGENT,
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "accept-language": "en-US,en;q=0.9",
        "x-li-lang": LI_LANG,
        "x-restli-protocol-version": "2.0.0",
        "x-li-track": _track_header(),
        "x-li-page-instance": page_instance(),
        "csrf-token": csrf_token(),
        # The fetch-metadata and client-hint headers a real Chrome sends. Absent
        # on a bare HTTP client, and their absence is trivially detectable.
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "priority": "u=1, i",
        "dnt": "1",
        "referer": "https://www.linkedin.com/feed/",
    }


# --- politeness --------------------------------------------------------------
# The LinkedIn account is the fragile resource here: a ban ends the service,
# and a public URL means strangers spend our quota. Defaults are conservative.

def cache_ttl() -> float:
    return _float("CACHE_TTL_SECONDS", 6 * 60 * 60)


def min_request_interval() -> float:
    return _float("MIN_REQUEST_INTERVAL", 3.0)


def daily_fetch_budget() -> int:
    return _int("DAILY_FETCH_BUDGET", 300)


def section_delay() -> float:
    """Gap between the nine per-section calls for one profile."""
    return _float("SECTION_DELAY", 2.0)


def request_timeout() -> float:
    return _float("REQUEST_TIMEOUT", 20.0)
