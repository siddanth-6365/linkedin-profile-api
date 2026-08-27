"""HTTP contract: status codes, the API key gate, and error mapping.

No network. `voyager.fetch_raw` is the seam — it is the only function that talks
to LinkedIn, so stubbing it exercises everything downstream of the request.
"""

import pytest
from fastapi.testclient import TestClient

from linkedin_profile_api import api, voyager


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Isolate from module-level state and from the developer's own .env.

    api.py calls config.load_env() at import, so a real .env on the machine
    running the tests would otherwise decide whether a session looks configured.
    That passed in CI (no .env there) and failed locally, which is precisely the
    wrong way round for a test to behave.
    """
    voyager._cache.clear()
    voyager._slug_locks.clear()
    voyager._budget.update(count=0)
    for name in ("LI_AT", "LI_JSESSIONID", "LI_COOKIE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("MIN_REQUEST_INTERVAL", "0")
    monkeypatch.setenv("SECTION_DELAY", "0")
    yield
    voyager._cache.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


@pytest.fixture
def stub_linkedin(monkeypatch, payloads):
    """Make fetch_raw answer with the fixture instead of calling LinkedIn."""
    primary, sections = payloads
    calls = []

    async def fake_fetch_raw(slug):
        calls.append(slug)
        return primary, sections, []

    monkeypatch.setattr(voyager, "fetch_raw", fake_fetch_raw)
    return calls


def test_healthz_reports_capability_without_leaking_secrets(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["linkedin_session_configured"] is False
    assert "cookie" not in str(body).lower()


def test_root_redirects_to_docs(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/docs"


def test_profile_returns_normalized_json(client, stub_linkedin):
    response = client.get("/profile", params={"url": "linkedin.com/in/adalovelace"})
    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["full_name"] == "Ada Lovelace"
    assert body["url"] == "https://www.linkedin.com/in/adalovelace/"
    assert body["_meta"]["cache"] == "miss"
    assert len(body["experience"]) == 2


def test_second_request_is_served_from_cache(client, stub_linkedin):
    url = {"url": "https://www.linkedin.com/in/adalovelace/"}
    assert client.get("/profile", params=url).json()["_meta"]["cache"] == "miss"
    assert client.get("/profile", params=url).json()["_meta"]["cache"] == "hit"
    assert len(stub_linkedin) == 1, "cache must spare the LinkedIn account a second fetch"


def test_refresh_bypasses_the_cache(client, stub_linkedin):
    url = {"url": "https://www.linkedin.com/in/adalovelace/"}
    client.get("/profile", params=url)
    body = client.get("/profile", params={**url, "refresh": "true"}).json()
    assert body["_meta"]["cache"] == "miss"
    assert len(stub_linkedin) == 2


def test_invalid_url_is_a_400_that_explains_itself(client):
    response = client.get("/profile", params={"url": "https://www.linkedin.com/company/x/"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_profile_url"
    assert "company page" in body["message"]


def test_missing_url_param_is_a_422(client):
    assert client.get("/profile").status_code == 422


def test_api_key_is_enforced_when_configured(client, stub_linkedin, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    params = {"url": "linkedin.com/in/adalovelace"}
    assert client.get("/profile", params=params).status_code == 401
    assert client.get("/profile", params=params, headers={"x-api-key": "wrong"}).status_code == 401
    ok = client.get("/profile", params=params, headers={"x-api-key": "s3cret"})
    assert ok.status_code == 200


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (voyager.SessionInvalid("cookie died"), 502, "linkedin_session_invalid"),
        (voyager.ProfileMissing("no such member"), 404, "profile_not_found"),
        (voyager.RateLimited("throttled", retry_after=300), 429, "rate_limited"),
        (voyager.NotConfigured("no session"), 503, "session_not_configured"),
        (voyager.VoyagerError("something else"), 502, "upstream_error"),
    ],
)
def test_upstream_failures_map_to_our_status_codes(
    client, monkeypatch, error, status, code
):
    async def boom(_slug):
        raise error

    monkeypatch.setattr(voyager, "fetch_raw", boom)
    response = client.get("/profile", params={"url": "linkedin.com/in/x"})
    assert response.status_code == status
    assert response.json()["error"] == code


def test_rate_limit_sets_retry_after(client, monkeypatch):
    async def boom(_slug):
        raise voyager.RateLimited("throttled", retry_after=300)

    monkeypatch.setattr(voyager, "fetch_raw", boom)
    response = client.get("/profile", params={"url": "linkedin.com/in/x"})
    assert response.headers["retry-after"] == "300"


def test_daily_budget_refuses_rather_than_risking_the_account(client, monkeypatch):
    monkeypatch.setenv("DAILY_FETCH_BUDGET", "0")
    monkeypatch.setenv("LI_AT", "x")
    monkeypatch.setenv("LI_JSESSIONID", '"ajax:1"')
    response = client.get("/profile", params={"url": "linkedin.com/in/x"})
    assert response.status_code == 429
    assert response.json()["error"] == "daily_budget_exhausted"


def test_debug_types_histograms_the_live_payload(client, stub_linkedin):
    body = client.get("/debug/types", params={"url": "linkedin.com/in/adalovelace"}).json()
    assert body["public_id"] == "adalovelace"
    assert body["types"]["com.linkedin.voyager.dash.identity.profile.Position"] == 3
    assert body["section_item_counts"]["experience"] == 2


@pytest.mark.parametrize("status", [200, 301, 302, 401, 403, 404, 429, 999, 410, 500])
def test_status_mapping_covers_every_voyager_answer(status):
    """A 3xx must map to session-invalid, not be followed to the login page.

    Following the redirect is what a naive client does; the auth wall then
    answers 403 — or 200 with an HTML page — and the real cause is lost.
    """
    import httpx

    response = httpx.Response(status, text="{}", request=httpx.Request("GET", "https://x/"))
    if status == 200:
        voyager._raise_for_status(response, "a call")  # must not raise
        return
    with pytest.raises(voyager.VoyagerError) as caught:
        voyager._raise_for_status(response, "a call")
    expected = {
        301: voyager.SessionInvalid, 302: voyager.SessionInvalid,
        401: voyager.SessionInvalid, 403: voyager.SessionInvalid,
        404: voyager.ProfileMissing,
        429: voyager.RateLimited, 999: voyager.RateLimited,
    }.get(status, voyager.VoyagerError)
    assert type(caught.value) is expected


def test_revoked_session_is_named_as_such():
    """LinkedIn expires the cookie it just rejected; say so, because re-pasting
    the same value cannot fix it and that is the obvious thing to try."""
    import httpx

    response = httpx.Response(
        302,
        headers=[("set-cookie", "li_at=delete me; Max-Age=0")],
        request=httpx.Request("GET", "https://x/"),
    )
    with pytest.raises(voyager.SessionInvalid, match="revoked"):
        voyager._raise_for_status(response, "a call")


def test_sections_are_fetched_serially_not_burst():
    """Concurrency here got a real session revoked mid-fetch. Guard the fix."""
    import inspect

    source = inspect.getsource(voyager._fetch_sections)
    assert "asyncio.gather" not in source, "section calls must not run concurrently"
    assert "section_delay" in source, "section calls must be spaced"


@pytest.mark.parametrize("total", [2, 25, 47])
def test_sections_follow_linkedin_pagination(monkeypatch, total):
    """Voyager pages sections at 20 and reports the real size in paging.total.

    Reading page one only would return a complete-looking answer that quietly
    drops everything past the twentieth entry.
    """
    import asyncio

    PAGE = 20

    async def fake_get(_client, _path, params, _what):
        start = int(params.get("start", 0))
        urns = [f"urn:li:fsd_profileSkill:(x,{i})" for i in range(start, min(start + PAGE, total))]
        return {
            "data": {"*elements": urns, "paging": {"start": start, "count": PAGE, "total": total}},
            "included": [
                {"entityUrn": urn, "$type": "...Skill", "name": f"skill{i}"}
                for i, urn in enumerate(urns, start=start)
            ],
        }

    monkeypatch.setattr(voyager, "_get", fake_get)
    monkeypatch.setenv("SECTION_DELAY", "0")
    merged, note = asyncio.run(voyager._fetch_section(None, "skills", "profileSkills", "urn:x"))

    assert note is None
    assert len(merged["data"]["*elements"]) == total, "every page must be collected"
    assert len(merged["included"]) == total
    assert len(set(e["entityUrn"] for e in merged["included"])) == total, "no duplicates"


def test_pagination_stops_and_says_so(monkeypatch):
    """A cap exists so a paging bug cannot loop forever; it must be reported."""
    import asyncio

    async def fake_get(_client, _path, params, _what):
        start = int(params.get("start", 0))
        return {
            "data": {
                "*elements": [f"urn:x:{i}" for i in range(start, start + 20)],
                "paging": {"start": start, "count": 20, "total": 10_000},
            },
            "included": [],
        }

    monkeypatch.setattr(voyager, "_get", fake_get)
    monkeypatch.setenv("SECTION_DELAY", "0")
    _merged, note = asyncio.run(voyager._fetch_section(None, "skills", "profileSkills", "urn:x"))
    assert note is not None and "stopped at" in note
