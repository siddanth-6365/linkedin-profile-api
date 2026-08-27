"""HTTP surface. Thin on purpose — the interesting code is in voyager/normalize."""

import secrets

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__, capture, config, normalize, urls, voyager

config.load_env()

app = FastAPI(
    title="LinkedIn Profile API",
    version=__version__,
    summary="Give it a LinkedIn profile URL, get structured JSON.",
    description=(
        "Reads LinkedIn's internal Voyager API with an authenticated session cookie. "
        "See /docs for parameters, and the repository README for the response schema, "
        "the approach, and the limitations."
    ),
)


def require_key(x_api_key: str | None) -> None:
    """Shared-secret check. Absent API_KEY disables it, which is fine locally.

    compare_digest rather than `==` so the check does not leak the key's prefix
    through response timing.
    """
    expected = config.api_key()
    if not expected:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthorized",
                "message": "Send the shared secret as the X-API-Key header.",
            },
        )


@app.exception_handler(voyager.VoyagerError)
async def voyager_error(_request: Request, exc: voyager.VoyagerError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.code, "message": exc.message},
        headers=headers,
    )


@app.exception_handler(urls.InvalidProfileURL)
async def invalid_url(_request: Request, exc: urls.InvalidProfileURL) -> JSONResponse:
    return JSONResponse(
        status_code=400, content={"error": "invalid_profile_url", "message": str(exc)}
    )


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/healthz", summary="Liveness, plus what the service can currently do")
async def healthz() -> dict:
    """Deliberately says whether a session is configured without revealing it."""
    return {
        "status": "ok",
        "version": __version__,
        "linkedin_session_configured": config.session_configured(),
        "api_key_required": bool(config.api_key()),
        "cache": voyager.cache_state(),
        "fetch_budget": voyager.budget_state(),
    }


@app.get("/profile", summary="A LinkedIn profile as structured JSON")
async def profile(
    url: str = Query(
        ...,
        description="A LinkedIn profile URL, e.g. https://www.linkedin.com/in/williamhgates/",
        examples=["https://www.linkedin.com/in/williamhgates/"],
    ),
    refresh: bool = Query(False, description="Bypass the cache and refetch from LinkedIn."),
    x_api_key: str | None = Header(None),
) -> dict:
    require_key(x_api_key)
    slug = urls.public_id(url)
    return await voyager.get_profile(slug, urls.canonical_url(slug), refresh=refresh)


@app.get("/debug/types", summary="What LinkedIn actually returned, by entity type")
async def debug_types(
    url: str = Query(..., description="A LinkedIn profile URL."),
    x_api_key: str | None = Header(None),
) -> dict:
    """A `$type` histogram of the live payloads, for when LinkedIn changes shape.

    Costs a full fetch against the daily budget and skips the cache, so it is a
    diagnostic rather than something to poll.
    """
    require_key(x_api_key)
    slug = urls.public_id(url)
    primary, sections, warnings = await voyager.fetch_raw(slug)
    return {
        "public_id": slug,
        "types": capture.type_histogram([primary, *sections.values()]),
        "section_item_counts": {
            key: len(normalize.section_entities(payload, normalize.merge_index([payload])))
            for key, payload in sorted(sections.items())
        },
        "warnings": warnings,
    }
