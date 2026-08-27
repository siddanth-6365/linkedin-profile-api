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

# ponytail: 5 pages = 100 entries per section, far past any real profile. A cap
# exists at all so a paging bug cannot loop against LinkedIn indefinitely.
MAX_SECTION_PAGES = 5


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
# ponytail: process-local. One Render instance, so a dict is the whole design;
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
    # LinkedIn revokes a session by 302-ing and expiring the cookie it just
    # rejected. Unmistakable, and worth its own message: the cookie is not stale,
    # it has been actively killed, and re-pasting the same value cannot work.
    if any("li_at=delete me" in header for header in response.headers.get_list("set-cookie")):
        raise SessionInvalid(
            f"LinkedIn has revoked this session (it expired the li_at cookie while answering "
            f"{what}). This usually follows automated access from an unfamiliar IP. Log in to "
            "the account in a browser, clear any security checkpoint, then copy a fresh li_at "
            "and JSESSIONID. Re-pasting the old value will not work."
        )
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


def _paging(payload) -> dict:
    data = payload.get("data") or {}
    return data.get("paging") or {}


async def _fetch_section(
    client: httpx.AsyncClient, key: str, resource: str, profile_urn: str
) -> tuple[dict, str | None]:
    """One section, following LinkedIn's pagination to the end.

    Voyager pages these at 20 (`paging.count`) and reports the true size in
    `paging.total`. Reading only the first page silently truncates anyone with
    more than twenty skills or positions — the kind of bug that looks like a
    complete answer, which is the worst kind. Pages are merged into one synthetic
    payload so the normalizer stays unaware that paging exists.
    """
    params = {"q": "viewee", "profileUrn": profile_urn}
    first = await _get(client, f"identity/dash/{resource}", params, f"the {key} section")

    elements = list(normalize.root_elements(first))
    included = list(first.get("included") or [])
    paging = _paging(first)
    total = paging.get("total")
    note = None

    page = 0
    while isinstance(total, int) and len(elements) < total and elements:
        page += 1
        if page > MAX_SECTION_PAGES:
            note = (
                f"{key}: stopped at {len(elements)} of {total} entries after "
                f"{MAX_SECTION_PAGES} pages, to limit pressure on the account."
            )
            break
        await asyncio.sleep(config.section_delay() + random.uniform(0, 0.4))  # noqa: S311
        nxt = await _get(
            client,
            f"identity/dash/{resource}",
            {**params, "start": len(elements), "count": paging.get("count") or 20},
            f"the {key} section (page {page + 1})",
        )
        more = normalize.root_elements(nxt)
        if not more:
            break
        elements.extend(more)
        included.extend(nxt.get("included") or [])

    merged = {
        "data": {"*elements": elements, "paging": {**paging, "fetched": len(elements)}},
        "included": included,
    }
    return merged, note


async def _fetch_sections(
    client: httpx.AsyncClient, profile_urn: str
) -> tuple[dict[str, dict], list[str]]:
    """The nine section calls, one at a time with a gap between them.

    An earlier version fired these three-at-a-time on the theory that a browser
    opening a profile also bursts. It does — but a browser has months of history
    on that IP, and a fresh account making the same burst from a datacenter got
    its session revoked mid-fetch (LinkedIn answered `set-cookie: li_at=delete
    me` partway through). Serial and spaced is slower and survives, which is the
    trade that matters when the account is the scarce resource.

    A section that fails degrades only itself.
    """
    warnings: list[str] = []
    payloads: dict[str, dict] = {}

    for index, (key, resource) in enumerate(config.SECTIONS.items()):
        if index:
            await asyncio.sleep(config.section_delay() + random.uniform(0, 0.6))  # noqa: S311
        try:
            payloads[key], note = await _fetch_section(client, key, resource, profile_urn)
            if note:
                warnings.append(note)
        except SessionInvalid:
            # The session died mid-fetch; the remaining calls would only add
            # pressure to an account that is already in trouble.
            warnings.append(
                f"{key} and any later section: abandoned — LinkedIn ended the session "
                "part-way through. The sections above are still valid."
            )
            break
        except VoyagerError as exc:
            warnings.append(f"{key}: not fetched — {exc.message}")

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
        # follow_redirects=False on purpose — see _raise_for_status. The jar is
        # seeded from config rather than pinned as a header so LinkedIn's own
        # set-cookie updates (lidc rotation) carry into the section calls.
        async with httpx.AsyncClient(
            timeout=config.request_timeout(),
            follow_redirects=False,
            cookies=config.cookies(),
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


async def check_session() -> dict:
    """Cheap liveness probe for the LinkedIn session itself: one call to /me.

    Worth having separately from /healthz, which only reports whether a cookie is
    *configured*. A revoked cookie is still configured, and finding that out from
    a reviewer's failed request is the wrong way round.

    Deliberately outside the daily budget: one request, and knowing the session
    is dead prevents far more traffic than it costs.
    """
    if not config.session_configured():
        return {"valid": False, "reason": "No LI_AT/LI_JSESSIONID (or LI_COOKIE) configured."}
    async with httpx.AsyncClient(
        timeout=config.request_timeout(), follow_redirects=False, cookies=config.cookies()
    ) as client:
        try:
            payload = await _get(client, "me", {}, "the session check")
        except VoyagerError as exc:
            return {"valid": False, "reason": exc.message, "error": exc.code}
    # /me nests the member differently across API versions, so look for the
    # shape (something with a name) rather than a fixed path.
    candidates = [payload.get("data") or {}, *(payload.get("included") or [])]
    candidates += [value for value in (payload.get("data") or {}).values() if isinstance(value, dict)]
    member = next(
        (c for c in candidates if isinstance(c, dict) and (c.get("firstName") or c.get("publicIdentifier"))),
        {},
    )
    name = " ".join(
        part.strip()
        for part in (member.get("firstName") or "", member.get("lastName") or "")
        if part.strip()
    )
    return {
        "valid": True,
        "authenticated_as": name or None,
        "public_id": member.get("publicIdentifier"),
    }
