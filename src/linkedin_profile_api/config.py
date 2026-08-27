"""Settings, plus the LinkedIn strings most likely to rot.

Every volatile value is env-overridable on purpose. When LinkedIn renames a
resource or bumps a decoration version, the fix should be a redeployed variable
rather than a code change — `identity/profiles/{id}/profileView` answering
410 Gone (it used to be *the* endpoint) is what that failure looks like.
"""

import os
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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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


def csrf_token() -> str:
    """LinkedIn's CSRF token is just JSESSIONID without its quotes."""
    return li_jsessionid().strip('"')


def session_configured() -> bool:
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

USER_AGENT = _env(
    "LI_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

LI_LANG = _env("LI_LANG", "en_US")


def headers() -> dict[str, str]:
    """Voyager rejects a request missing any of these.

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
        "csrf-token": csrf_token(),
        # Always re-quote JSESSIONID: the browser cookie carries quotes, and
        # accepting either paste style removes the commonest setup mistake.
        "cookie": f'li_at={li_at()}; JSESSIONID="{csrf_token()}"',
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
