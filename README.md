# LinkedIn Profile API

Give it a LinkedIn profile URL, get the profile back as structured JSON.

```bash
curl -s -H "x-api-key: $API_KEY" \
  "$BASE_URL/profile?url=https://www.linkedin.com/in/williamhgates/"
```

It reads LinkedIn's internal **Voyager** API — the same private API linkedin.com's own
front end calls — using an authenticated session cookie. There is no official API for
this: LinkedIn's public Profile API only ever returns *your own* profile, and only after
an OAuth partner review.

- **Live:** `<LIVE_URL>` · interactive docs at `<LIVE_URL>/docs`
- **Full example response:** [docs/example-response.json](docs/example-response.json)

---

## Contents

- [Quick start](#quick-start) · [Getting the cookies](#getting-the-cookies)
- [API reference](#api-reference) · [Response schema](#response-schema)
- [Approach](#approach-how-this-was-reverse-engineered) — the interesting part
- [Protecting the account](#protecting-the-linkedin-account)
- [Known limitations](#known-limitations) · [Tests](#tests) · [Deploy](#deploy)

---

## Quick start

```bash
git clone https://github.com/siddanth-6365/linkedin-profile-api
cd linkedin-profile-api
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"

cp .env.example .env      # then paste your cookies in — .env is gitignored
./.venv/bin/uvicorn linkedin_profile_api.api:app --reload
```

```bash
curl -s 'http://127.0.0.1:8000/profile?url=https://www.linkedin.com/in/williamhgates/' | jq
```

Or with Docker:

```bash
docker build -t linkedin-profile-api . && docker run --rm -p 8000:8000 --env-file .env linkedin-profile-api
```

`GET /healthz` tells you whether a session is configured before you try a real fetch.

### Getting the cookies

1. Log in to linkedin.com **in a burner account** (see [limitations](#known-limitations)).
2. DevTools → Application → Cookies → `https://www.linkedin.com`.
3. Copy two values into `.env`:
   - `LI_AT` — the `li_at` cookie. This is the session; treat it like a password.
   - `LI_JSESSIONID` — the `JSESSIONID` cookie, e.g. `"ajax:1234567890123456789"`.
     Quotes optional; the service normalises either form.

The `csrf-token` header is derived from `JSESSIONID` automatically. If the two ever
disagree LinkedIn answers `403 CSRF check failed`, which is the single commonest way to
get this wrong by hand.

---

## API reference

Also available as OpenAPI at `/docs` (Swagger) and `/redoc`.

### `GET /profile`

| Parameter | Type | Description |
| --- | --- | --- |
| `url` | string, **required** | A LinkedIn profile URL. Tolerant of trailing slashes, `?originalSubdomain=`, `/details/experience/` sub-paths, locale hosts (`in.linkedin.com`), `/mwlite/in/`, percent-encoded unicode vanity names, and legacy `/pub/` URLs. A bare slug (`williamhgates`) also works. |
| `refresh` | bool, default `false` | Bypass the cache and refetch from LinkedIn. Costs budget; use sparingly. |
| `X-API-Key` | header | Required when `API_KEY` is set on the server. |

Returns `200` with the [response schema](#response-schema) below.

### `GET /healthz`

Liveness plus current capability — whether a session is configured, whether a key is
required, cache size, and today's remaining fetch budget. Never echoes a secret.

### `GET /debug/types`

A `$type` histogram of the live Voyager payloads for one profile, plus per-section item
counts. This is the diagnostic for "LinkedIn changed something" — it shows what actually
came back rather than what the parser made of it. Costs a full fetch against the daily
budget and skips the cache, so it is not for polling.

### Errors

Every error is `{"error": "<code>", "message": "<what to do about it>"}`.

| Status | `error` | Meaning |
| --- | --- | --- |
| 400 | `invalid_profile_url` | Not a member profile URL. The message names what it was instead (a company page, a post, …). |
| 401 | `unauthorized` | Missing or wrong `X-API-Key`. |
| 404 | `profile_not_found` | LinkedIn has no such profile, or it is invisible to this session. |
| 422 | — | Missing `url` parameter (FastAPI validation). |
| 429 | `rate_limited` | LinkedIn is throttling us (its HTTP 429 or its own `999`). Sets `Retry-After`. |
| 429 | `daily_budget_exhausted` | Our own guard rail tripped, not LinkedIn's. Sets `Retry-After`. |
| 502 | `linkedin_session_invalid` | The `li_at` cookie expired. Refresh it; see [limitations](#known-limitations). |
| 502 | `upstream_error` | LinkedIn returned something unexpected — including `410 Gone`, meaning an endpoint was withdrawn. |
| 503 | `session_not_configured` | No `LI_AT`/`LI_JSESSIONID` set on the server. |

---

## Response schema

Abbreviated below; the complete example is
[docs/example-response.json](docs/example-response.json).

```jsonc
{
  "url": "https://www.linkedin.com/in/adalovelace/",
  "public_id": "adalovelace",
  "urn": "urn:li:fsd_profile:ACoAAAtest01",
  "fetched_at": "2026-08-27T09:28:23+00:00",

  "profile": {
    "first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace",
    "headline": "Mathematician | Analytical Engine notes",
    "about": "Wrote the first algorithm intended for a machine.",
    "location": { "name": "London, England, United Kingdom", "country": "GB",
                  "postal_code": "SW1A 1AA" },
    "industry": "Software Development",
    "pronouns": "She Her",
    "premium": true, "influencer": false, "open_to_work": true,
    "follower_count": 4212,
    "images": {
      "profile_picture": {
        "url": "https://media.licdn.com/dms/image/v2/PROFILE/photo_800.jpg",  // largest
        "sizes": [ { "width": 100, "height": 100, "url": "…photo_100.jpg" } /* , … */ ]
      },
      "background": { "url": "…cover_1400.jpg", "sizes": [ /* … */ ] }
    }
  },

  "experience": [
    {
      "title": "Principal Mathematician",
      "company": "Analytical Engine Works",
      "company_urn": "urn:li:fsd_company:1441",
      "company_url": "https://www.linkedin.com/company/analytical-engine-works/",
      "company_logo": { "url": "…logo_200.png", "sizes": [ /* … */ ] },
      "employment_type": "Full Time",
      "location": "London, United Kingdom",
      "description": "Notes on the Engine, including Note G.",
      "start": { "year": 2020, "month": 6, "day": null, "text": "2020-06" },
      "current": true,
      "duration_months": 75
    }
  ],

  "education":      [ { "school", "school_urn", "school_logo", "degree", "field_of_study",
                        "grade", "activities", "description", "start", "end" } ],
  "skills":         [ { "name": "Algorithms", "endorsement_count": 99 } ],
  "certifications": [ { "name", "authority", "authority_logo", "license_number",
                        "url", "start", "end" } ],
  "languages":      [ { "name": "English", "proficiency": "Native Or Bilingual" } ],
  "projects":       [ { "title", "description", "url", "start", "end" } ],
  "honors":         [ { "title", "issuer", "description", "date" } ],
  "volunteer":      [ { "role", "organization", "cause", "description", "start", "end" } ],
  "publications":   [ { "name", "publisher", "description", "url", "date" } ],

  "_meta": {
    "source": "linkedin.voyager.identity.dash",
    "section_counts": { "experience": 2, "skills": 2, /* … */ },
    "warnings": [ "projects: LinkedIn returned no entries — either the profile has none, or it is not visible to this account." ],
    "cache": "miss",
    "elapsed_ms": 4127
  }
}
```

Three deliberate choices in there:

**Dates keep LinkedIn's precision.** A position gives a year and a month and no day, so
the object carries `year`, `month`, `day: null`, and a `text` rendering that stops where
the data stops (`"2020-06"`, or just `"2012"` for a year-only education). No day is
invented to make the field parse as a date.

**Absent fields are omitted, not nulled.** If LinkedIn has no `grade` for an education
entry, the key is missing rather than `null`, so key presence means something. The one
exception is `day` inside a date, where `null` states positively that LinkedIn had no day.

**`_meta.warnings` makes a thin answer visibly thin.** An empty `projects` array could
mean the person has no projects, or that the section call failed, or that this session
cannot see it. The warning distinguishes those, and `section_counts` only lists sections
that were actually fetched. A partial result is never dressed up as a complete one.

The response is assembled as plain dicts rather than validated through strict response
models on the way out. That is on purpose: this is an undocumented upstream, and a strict
model meeting one unexpected field shape would turn an otherwise-good profile into a 500.

---

## Approach: how this was reverse-engineered

### Establishing what still exists, before writing any code

Nearly every guide to this problem — and the best-known Python library for it, since
withdrawn — reaches for `GET /voyager/api/identity/profiles/{id}/profileView`, one call
that returned an entire profile. It does not work any more.

That was worth establishing *first*, and it can be established without credentials. Send
a request with a `csrf-token` header that matches a dummy `JSESSIONID` cookie and no
`li_at`: a route that exists gets far enough to complain about authentication, and a route
that does not exist 404s. A control probe confirms the method — `identity/dash/totallyFakeResource`
answers `404`, so a `401` really does mean "this route is alive and wants a session":

| Probe | Result | Read as |
| --- | --- | --- |
| `identity/dash/totallyFakeResource` | `404` | control — nonexistent route |
| `identity/profiles/{id}/profileView` | **`410 Gone`** | **withdrawn** |
| `identity/dash/profiles?q=memberIdentity` | `401` | alive → primary call |
| `identity/dash/profilePositions` and 9 more, `?q=viewee&profileUrn=` | `401` ×10 | alive → section calls |
| `/voyager/api/graphql` with a bogus `queryId` | `403` / `500` | alive, but needs a server-registered query id |
| profile page, no cookie, browser UA | **`999` + authwall** | no unauthenticated path |

Four decisions fell out of that table before a line of code was written:

1. **Don't build on `profileView`.** It is gone. The cost is that one call becomes ten.
2. **Don't build a GraphQL client.** `/voyager/api/graphql` needs a `queryId` that LinkedIn
   registers server-side and ships inside its own JS bundles. Scraping those ids is the
   most fragile part of every implementation that does it, and the REST `identity/dash`
   resources return everything required, so there is no reason to take that on.
3. **Don't build an unauthenticated fallback.** A guest profile fetch from a datacenter IP
   returns HTTP `999` and an authwall. A JSON-LD scrape of the public page is a popular
   suggestion; it cannot work from a server, so it would have been dead code.
4. **Authentication is `li_at` + `JSESSIONID`, with `csrf-token` mirroring `JSESSIONID`.**
   Getting that pairing wrong yields exactly `403 CSRF check failed`, which is a useful,
   distinguishable signal.

### The header that shapes the whole design

Voyager honours `accept: application/vnd.linkedin.normalized+json+2.1`, and that changes
what you get back:

```jsonc
{
  "data":     { "*elements": ["urn:li:fsd_profilePosition:(…,1)", "…"] },  // order
  "included": [ { "entityUrn": "…", "$type": "…Position", "title": "…" }, … ] // flat table
}
```

Rather than a nested tree, the payload is a **flat entity table** plus a list of URN
references giving order — Rest.li's normalized form, essentially a tiny relational
document. So the parser never walks a JSON path like
`data.positionView.elements[0].companyName`. It merges every response's `included` into
one `entityUrn → entity` index and reads each section through that response's own
`*elements` list.

Three things follow, and they are the reason the design is shaped this way:

- **Nesting changes stop mattering.** Path-walking parsers rot when LinkedIn reshapes a
  decoration. An index lookup does not care about shape.
- **The ten responses merge for free.** The primary call and all nine section calls
  normalize through identical code, because a merged index does not care which endpoint
  supplied an entity. On a URN collision the richer entity wins, which matters: the
  primary call returns *truncated stubs* of the same positions the section calls return in
  full, exactly as the web UI shows three roles behind a "Show all 42" link.
- **Cross-references resolve through the same index.** `position → *company → logo` is
  two lookups, so company URLs and logos come out without special-casing.

Field *names* inside an entity are still LinkedIn's, and those do drift, so every read
goes through a `pick()` helper with several candidate spellings (`title`/`name`,
`dateRange`/`timePeriod`, `schoolName` then the dereferenced school's `name`). Images get
similar treatment: the vector-image block is nested differently on a profile picture, a
company logo, and a school logo, so the code searches for the *shape*
(`{rootUrl, artifacts}`) instead of hard-coding three paths, then builds absolute URLs as
`rootUrl + fileIdentifyingUrlPathSegment` and returns every size.

### Fetch plan

1. `identity/dash/profiles?q=memberIdentity&memberIdentity=<slug>` → core profile and its
   `urn:li:fsd_profile:…`. If the `decorationId` version is stale, retry undecorated.
2. Nine `identity/dash/profile<Section>?q=viewee&profileUrn=<urn>` calls for full lists,
   in a small burst of three — opening a real profile page fires a burst of Voyager calls
   too, so this looks more like a browser than nine calls spaced minutes apart.
3. Merge every `included`, normalize once.

A section call that fails degrades only that section and records a warning; it never
fails the whole response. Getting nine sections and losing `languages` is a good outcome
worth returning.

### What is designed to be changed without a code change

The endpoint table, decoration id, user agent and locale all live in
[`config.py`](src/linkedin_profile_api/config.py) and are environment-overridable. When
LinkedIn bumps `FullProfileWithEntities-93` to `-94`, the fix should be a redeployed
variable, not a patch release. `/debug/types` is the tool for working out *what* to
change, and `python -m linkedin_profile_api.capture <url>` does the same offline while
snapshotting the raw payloads to `captures/` (gitignored — they hold real profile data).

---

## Protecting the LinkedIn account

The account is the fragile resource. A ban ends the service, the deployed URL is public,
and datacenter IPs get throttled fast — a couple of dozen unauthenticated probes from a
laptop were enough to start drawing HTTP `999`s during development. So:

| Guard | Default | Why |
| --- | --- | --- |
| TTL cache, keyed by public id | 6 h | Repeat lookups never touch LinkedIn at all. |
| Per-profile single-flight lock | — | Simultaneous requests for one profile cost one fetch, not two. |
| Serialized fetches, min interval + jitter | 3 s | One conversation with LinkedIn at a time, and not metronomic. |
| Daily fetch budget | 300/day | A bug or a crawler cannot burn the account overnight. Cached profiles keep serving. |
| `X-API-Key` on `/profile` | required in prod | Otherwise a stranger spends your quota. |

All of it is process-local — a dict, a lock, a counter. That is deliberate for a
single-instance deployment, and it is the one thing that would need replacing (with Redis)
before running more than one replica, since each replica would otherwise keep its own
cache and its own budget.

---

## Known limitations

**Burner-account visibility.** A fresh account with no network sees less of a stranger's
profile than a well-connected one does, and LinkedIn may serve out-of-network profiles in
a restricted form. So a missing field is sometimes LinkedIn's decision rather than a
parser bug — `_meta.warnings` is what tells the two apart. New accounts may also hit an
email or phone verification checkpoint on first login.

**Cookie expiry.** `li_at` lasts roughly a year; `JSESSIONID` is per-session, and both die
early if LinkedIn invalidates the session (a password change, a security prompt). The
symptom is `502 linkedin_session_invalid`; the fix is re-pasting both cookies. This is an
operational chore, not a bug that can be fixed in code.

**It is an unofficial API.** Endpoints, decoration versions and field names can change or
vanish without notice, and `profileView` returning `410` is that having already happened
once. The type-driven parser, the candidate-spelling reads and the env-overridable
endpoint table are hedges, not guarantees. `/debug/types` is the first thing to look at
when output goes thin.

**Rate limits and scale.** This is built for interactive, low-volume lookups. Bulk
scraping would need residential proxies and a pool of accounts, which is out of scope
here; the honest mitigation at this scale is caching plus conservative pacing.

**Not implemented.** Recommendations, endorsement detail, contact info, posts and activity
are all reachable through neighbouring Voyager resources but were not part of the brief. A
headless-browser fallback is the upgrade path if LinkedIn ever locks down the `dash`
resources — it was skipped deliberately, since all ten are currently alive and Playwright
would roughly quadruple the image size for a path that may never run.

**Terms of service.** Automated collection of profile data is against LinkedIn's User
Agreement, and their position on it has been litigated more than once. This is a technical
exercise built with a burner account at hand-scale volume; it is not something to point at
production data or at anyone else's account.

---

## Tests

```bash
./.venv/bin/ruff check . && ./.venv/bin/pytest -q
```

63 tests, no credentials and no network needed — `voyager.fetch_raw` is the only function
that talks to LinkedIn, so stubbing that one seam exercises everything else. CI runs the
suite on Python 3.11, 3.12 and 3.13.

What the suite is actually protecting, since these are the failures that would matter:

- **URL parsing** — 16 accepted forms and 11 rejected ones, including
  `linkedin.com.evil.example` and percent-encoded unicode slugs (decoding twice is how
  those break).
- **Truncated stubs losing to full entities** — if the merge ever preferred the primary
  call's stub, every profile would silently lose the detail it came for.
- **A missing `*elements` list** — the `languages` fixture omits it, which is the shape a
  decoration change takes; the fallback path must still find the entities.
- **Ordering, partial dates, absent-vs-zero** — an absent endorsement count must not
  become `0`, and `duration_months` must count an open-ended range up to today.
- **Error mapping** — every upstream failure lands on the documented status code, the
  budget guard refuses rather than risking the account, and the cache spares a second fetch.

The fixture in `tests/fixtures/` is synthetic, in the exact shape Voyager returns. It uses
the same file layout `capture.py --out` writes, so a real capture can be dropped in as a
regression test. No real profile data is committed.

---

## Deploy

Railway, from this repo's `Dockerfile`:

```bash
npm i -g @railway/cli && railway login
railway init && railway up
```

Then set `LI_AT`, `LI_JSESSIONID` and `API_KEY` in **Railway's dashboard** (Variables tab)
rather than via `railway variables --set`, which would leave the session cookie in your
shell history. Generate a public domain under Settings → Networking; `railway.json` points
the health check at `/healthz`.

The container reads Railway's injected `$PORT` and binds `0.0.0.0`, and runs as a
non-root user.

### Secrets

Nothing sensitive is in this repository: `.env` and `captures/` are gitignored, `.env.example`
carries only empty keys, and the test suite needs no credentials. Every secret is supplied
as an environment variable at runtime.

---

## License

MIT. Built as a hiring-challenge exercise.
