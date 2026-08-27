"""Authenticated Voyager client, and everything that keeps the account alive.

Fetch plan for one profile:

  1. GET identity/dash/profiles?q=memberIdentity&memberIdentity=<slug>
     -> the core profile, and its `urn:li:fsd_profile:...` entityUrn.
  2. GET identity/dash/profile<Section>?q=viewee&profileUrn=<urn> for each of the
     nine sections. The primary call truncates entity lists the way the web UI
     does ("Show all 42 experiences"), so full lists need their own calls.
  3. Merge every `included` array and normalize once.

The old one-shot `identity/profiles/<id>/profileView` is gone — it answers
410 — which is why this is ten calls rather than one.

The LinkedIn account is the resource worth protecting: a ban ends the service,
and the deployed URL is public. Hence the cache, the spacing between fetches,
and the daily budget, all in this module because they exist only to shield
upstream.
"""

import asyncio
import random
import time
from datetime import date

import httpx

from . import config, normalize


class VoyagerError(Exception):
    """Upstream failure, carrying the status we should answer with."""

    status = 502
    code = "upstream_error"
    retry_after: int | None = None

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        if retry_after is not None:
            self.retry_after = retry_after


class SessionInvalid(VoyagerError):
    status, code = 502, "linkedin_session_invalid"


class ProfileMissing(VoyagerError):
    status, code = 404, "profile_not_found"


class RateLimited(VoyagerError):
    status, code = 429, "rate_limited"


class BudgetExhausted(VoyagerError):
    status, code = 429, "daily_budget_exhausted"


class NotConfigured(VoyagerError):
    status, code = 503, "session_not_configured"


# --- politeness state --------------------------------------------------------
# ponytail: process-local. One Railway instance, so a dict is the whole design;
# Redis is the upgrade path the day this runs more than one replica.

_cache: dict[str, tuple[float, dict]] = {}
_fetch_gate = asyncio.Lock()
_last_fetch = 0.0
_budget = {"day": date.min, "count": 0}
_slug_locks: dict[str, asyncio.Lock] = {}


def _cache_get(slug: str) -> dict | None:
    entry = _cache.get(slug)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at < time.monotonic():
        _cache.pop(slug, None)
        return None
    return payload


def _cache_put(slug: str, payload: dict) -> None:
    _cache[slug] = (time.monotonic() + config.cache_ttl(), payload)


def _spend_budget() -> None:
    today = date.today()
    if _budget["day"] != today:
        _budget.update(day=today, count=0)
    budget = config.daily_fetch_budget()
    if _budget["count"] >= budget:
        raise BudgetExhausted(
            f"Daily budget of {budget} LinkedIn fetches is spent; it resets at 00:00 UTC. "
            "Cached profiles still serve. Raise DAILY_FETCH_BUDGET to lift this.",
            retry_after=3600,
        )
    _budget["count"] += 1


def budget_state() -> dict:
    return {"spent_today": _budget["count"], "budget": config.daily_fetch_budget()}


def cache_state() -> dict:
    return {"entries": len(_cache), "ttl_seconds": config.cache_ttl()}


# --- HTTP --------------------------------------------------------------------

def _raise_for_status(response: httpx.Response, what: str) -> None:
    status = response.status_code
    if status < 300:
        return
    if 300 <= status < 400:
        # Voyager redirects to the login page when the session is dead. Never
        # follow it: the auth wall answers with a 403, or worse a 200 HTML page,
        # either of which would be reported as some unrelated failure.
        raise SessionInvalid(
            f"LinkedIn redirected {what} to its login page (HTTP {status}), which means the "
            "session is not valid. Refresh li_at and JSESSIONID from a logged-in browser."
        )
    if status in (401, 403):
        raise SessionInvalid(
            f"LinkedIn rejected the session on {what} (HTTP {status}). The li_at cookie has "
            "expired or JSESSIONID and the csrf-token disagree — refresh both from a "
            "logged-in browser session."
        )
    if status == 404:
        raise ProfileMissing(f"LinkedIn has no such profile ({what} returned 404).")
    if status in (429, 999):
        # 999 is LinkedIn's own throttle/deny code. Backing off is the only move.
        raise RateLimited(
            f"LinkedIn is throttling this session or IP (HTTP {status} on {what}). "
            "Wait before retrying; repeated pressure gets the account restricted.",
            retry_after=300,
        )
    if status == 410:
        raise VoyagerError(
            f"LinkedIn has withdrawn {what} (HTTP 410 Gone). The endpoint table in "
            "config.py needs updating."
        )
    raise VoyagerError(f"LinkedIn returned HTTP {status} on {what}.")


async def _get(client: httpx.AsyncClient, path: str, params: dict, what: str) -> dict:
    try:
        response = await client.get(
            f"{config.VOYAGER_BASE}/{path}", params=params, headers=config.headers()
        )
    except httpx.HTTPError as exc:
        raise VoyagerError(f"Could not reach LinkedIn for {what}: {exc}") from exc
    _raise_for_status(response, what)
    try:
        return response.json()
    except ValueError as exc:
        raise VoyagerError(f"LinkedIn returned non-JSON for {what}.") from exc


async def _fetch_primary(client: httpx.AsyncClient, slug: str) -> dict:
    """Core profile. Retries without decorationId if the version is stale."""
    params = {
        "q": "memberIdentity",
        "memberIdentity": slug,  # httpx encodes this once; the slug is stored decoded
        "decorationId": config.PROFILE_DECORATION,
    }
    try:
        return await _get(client, "identity/dash/profiles", params, "the profile lookup")
    except VoyagerError as exc:
        if not isinstance(exc, SessionInvalid | RateLimited | ProfileMissing):
            params.pop("decorationId")
            return await _get(
                client,
                "identity/dash/profiles",
                params,
                "the profile lookup (undecorated retry)",
            )
        raise


async def _fetch_sections(
    client: httpx.AsyncClient, profile_urn: str
) -> tuple[dict[str, dict], list[str]]:
    """The nine section calls. A section that fails degrades only itself.

    Opening a real profile page fires a burst of Voyager calls, so a small burst
    here looks more like a browser than nine calls spaced minutes apart. The
    per-*profile* spacing is enforced by the caller.
    """
    warnings: list[str] = []
    payloads: dict[str, dict] = {}
    gate = asyncio.Semaphore(3)

    async def one(key: str, resource: str) -> None:
        async with gate:
            await asyncio.sleep(random.uniform(0.1, 0.5))  # noqa: S311 — jitter, not crypto
            try:
                payloads[key] = await _get(
                    client,
                    f"identity/dash/{resource}",
                    {"q": "viewee", "profileUrn": profile_urn},
                    f"the {key} section",
                )
            except VoyagerError as exc:
                warnings.append(f"{key}: not fetched — {exc.message}")

    await asyncio.gather(*(one(key, res) for key, res in config.SECTIONS.items()))
    return payloads, warnings


async def _wait_turn() -> None:
    """Space consecutive LinkedIn fetches, with jitter so we aren't metronomic."""
    global _last_fetch
    interval = config.min_request_interval()
    elapsed = time.monotonic() - _last_fetch
    if elapsed < interval:
        await asyncio.sleep(interval - elapsed + random.uniform(0, 0.5))  # noqa: S311
    _last_fetch = time.monotonic()


async def fetch_raw(slug: str) -> tuple[dict, dict[str, dict], list[str]]:
    """The raw payloads for one profile: (primary, {section: payload}, warnings).

    Everything that shields the LinkedIn account lives here — the budget, the
    spacing, the single-conversation lock — so no caller can skip it.
    """
    if not config.session_configured():
        raise NotConfigured(
            "No LinkedIn session configured. Set LI_AT and LI_JSESSIONID "
            "(see .env.example) and restart."
        )

    async with _fetch_gate:  # one LinkedIn conversation at a time
        _spend_budget()
        await _wait_turn()
        # follow_redirects=False on purpose — see _raise_for_status.
        async with httpx.AsyncClient(
            timeout=config.request_timeout(), follow_redirects=False
        ) as client:
            primary = await _fetch_primary(client, slug)
            profile = normalize.find_profile(primary, normalize.merge_index([primary]))
            if profile is None or not profile.get("entityUrn"):
                raise ProfileMissing(
                    f"LinkedIn answered for {slug!r} but included no profile. The account "
                    "may be private, restricted, or out of this session's network."
                )
            sections, warnings = await _fetch_sections(client, profile["entityUrn"])
    return primary, sections, warnings


async def get_profile(slug: str, url: str, refresh: bool = False) -> dict:
    """Cached, throttled, budgeted profile fetch. Returns the response body."""
    # Per-slug lock so simultaneous requests for one profile cost one fetch: the
    # waiter re-checks the cache and finds it filled rather than spending budget.
    lock = _slug_locks.setdefault(slug, asyncio.Lock())
    try:
        async with lock:
            return await _get_profile(slug, url, refresh)
    finally:
        if not lock.locked():
            _slug_locks.pop(slug, None)


async def _get_profile(slug: str, url: str, refresh: bool) -> dict:
    if not refresh:
        cached = _cache_get(slug)
        if cached is not None:
            return {**cached, "_meta": {**cached["_meta"], "cache": "hit"}}

    started = time.monotonic()
    primary, sections, warnings = await fetch_raw(slug)
    try:
        result = normalize.normalize(slug, url, primary, sections, warnings)
    except normalize.ProfileNotFound as exc:
        raise ProfileMissing(str(exc)) from exc

    result["_meta"]["cache"] = "miss"
    result["_meta"]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    _cache_put(slug, result)
    return result
