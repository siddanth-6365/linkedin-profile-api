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

Verified end to end against live LinkedIn: all ten calls succeeding, nine of nine
sections fetched with zero failures, names, headlines, about text, locations and
industries resolved through URN references, profile and banner images in four size
variants each, and company logos on every position.

### No browser, anywhere

This is pure HTTP against LinkedIn's own endpoints. Nothing is rendered, driven or
scraped from a page:

| | |
| --- | --- |
| Runtime dependencies | `httpx`, `fastapi`, `uvicorn` — the entire list |
| Browser automation (Playwright, Selenium, Puppeteer, webdriver, nodriver, cloudscraper) | none present |
| HTML parsers (BeautifulSoup, lxml, html5lib) | none present |
| `subprocess` / shell-out | none |
| Outbound requests | one base URL, `https://www.linkedin.com/voyager/api` |
| Parsing | `response.json()` only — no HTML is parsed at any point |

The container is a `python:3.13-slim` image with three packages in it; there is no browser
to launch. Verify with `grep -riE 'playwright|selenium|puppeteer|webdriver|bs4|lxml' src/`.

A browser is used exactly once, by a human, outside the service: logging in to copy a
session cookie into configuration — which is what "you may use your own LinkedIn
credentials in the backend" describes. No browser exists in the request path, and none is
launched, driven or embedded at any point.

---

## Contents

- [Quick start](#quick-start) · [Getting the cookies](#getting-the-cookies)
- [API reference](#api-reference) · [Response schema](#response-schema)
- [Session handling](#session-handling) — how long a cookie lasts, and why login is not automated
- [Why not the official API?](#why-not-the-official-api) — the question this answers first
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

**Better still, set `LI_COOKIE` instead of those two.** Copy the entire `Cookie` request
header from any linkedin.com request (DevTools → Network → a request → Request Headers →
Cookie) and paste it as one value. It takes precedence, it cannot disagree with itself,
and sessions survive longer when the request carries the same cookies the browser did —
see [session handling](#session-handling).

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

### `GET /session`

Whether the configured LinkedIn cookie still works, and which account it belongs to. See
[session handling](#session-handling) — a revoked cookie is still a configured cookie, so
`/healthz` alone cannot tell you. One upstream request, outside the daily budget.

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

## Session handling

The single operational question this project has, so it gets its own section.

### How long does a session last?

`li_at` is issued with about a year of nominal expiry. That number is close to
meaningless. Sessions here do not die of old age, they get **revoked** — and the
signature is unambiguous:

```
HTTP/2 302
set-cookie: li_at=delete me; Max-Age=0
```

LinkedIn expiring the cookie it just accepted. During development a fresh cookie was
revoked roughly ninety seconds after being issued, mid-fetch, on a brand-new account
calling from an unfamiliar IP. Revocation follows behaviour, not time:

| Trigger | Mitigation here |
| --- | --- |
| Bursty request patterns | Section calls run serially, `SECTION_DELAY` apart; profile fetches are spaced and jittered |
| Requests unlike the browser the cookie came from | `LI_COOKIE` sends the browser's whole cookie header; the client keeps a jar so LinkedIn's `lidc` rotation is honoured |
| IP or geography changing | Nothing to be done without a proxy — see below |
| Repeated logins from new devices | Do not automate login; see below |
| Volume | 6 h cache, daily budget |

### So: refresh it, or regenerate it every time?

**Refresh it manually, rarely.** Regenerating more often actively hurts — frequent logins
from a datacenter IP are themselves one of the strongest revocation signals, and a new
cookie carries exactly the same nominal expiry as the old one. There is nothing to gain.

### Why login is not automated

LinkedIn's internal `/uas/authenticate` endpoint exists, and older scraper libraries
posted credentials to it. This project deliberately does not, for three reasons that have
nothing to do with difficulty:

1. **It usually fails anyway.** Automated login from a datacenter IP lands on
   `/checkpoint/challenge` — a CAPTCHA, or an email/SMS PIN. A cold start that needs a
   human to solve a CAPTCHA is not a working deployment.
2. **It inverts the security posture.** A session cookie is revocable and narrow. A
   password stored in a hosting provider's environment grants full account control,
   including changing the password and the recovery email.
3. **It increases the thing it was meant to avoid.** More logins means more revocations.

The trade is one manual paste that lasts weeks against an automated flow that trips a
challenge on every restart and risks a permanent account restriction.

Commercial services do solve this properly (Unipile, Prospeo, Bright Data and others
manage LinkedIn account connections, checkpoints and residential egress). That is the
correct answer for a product; it costs money and sidesteps the reverse-engineering this
exercise is about.

### Knowing before your users do

`GET /healthz` reports whether a cookie is *configured*. A revoked cookie is still
configured, so it also needs:

```bash
curl -s -H "x-api-key: $API_KEY" "$BASE_URL/session"
```

```jsonc
{ "valid": true, "authenticated_as": "Jane Doe", "public_id": "janedoe" }
// or
{ "valid": false, "error": "linkedin_session_invalid",
  "reason": "LinkedIn has revoked this session (it expired the li_at cookie …)" }
```

One request, outside the daily budget, because discovering a dead session from a failed
user request is the wrong way round. When it reads `false`, log in via a browser, clear any
checkpoint, and paste a fresh `LI_COOKIE` into the host's dashboard.

### Deploying to a different country from where you logged in

Worth stating plainly: a cookie created on a home connection and replayed from a cloud
region on another continent is a textbook revocation trigger. A single-instance demo with
periodic manual refreshes is the honest shape of this without residential proxies, and
that is what this is.

---

## Why not the official API?

LinkedIn does have official APIs — [Accessing LinkedIn APIs](https://www.linkedin.com/help/linkedin/answer/a526048)
— and they cannot do this. Worth stating up front, because it is the first question this
project should have to answer.

| Official route | What it returns | Whose data |
| --- | --- | --- |
| [Sign In with LinkedIn (OIDC)](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2), self-service | `sub`, `name`, `given_name`, `family_name`, `picture`, `locale`, `email`. Seven fields — no experience, education, skills, certifications, about or location. | Only the member who just signed into *your* app. |
| [Profile API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/profile-api), enterprise/partner | More fields, but access is "restricted to those developers approved by LinkedIn", each call is made "on behalf of a user" via OAuth, and "you may never store data returned from the Profile API for members other than the authenticated member". | Only members who granted *your* app consent. |

**No official endpoint, at any tier or any price, accepts an arbitrary profile URL and
returns that person's profile.** That capability does not exist officially; preventing it
is what LinkedIn has litigated over (*hiQ Labs v. LinkedIn*). So the brief — accept a
profile URL, return what is on the profile page — is not satisfiable through official
channels. This is not the unofficial route chosen for convenience; it is the only route
that answers the question asked.

That leaves the private API linkedin.com's own front end calls. **Voyager is not a third
option or a semi-official product: it is that front end's backend.** Loading a profile in
a browser fires a burst of `/voyager/api/...` requests carrying your session cookie.
Reverse-engineering it means reading those calls and replaying them server-side — the same
mechanism behind the PhantomBuster scraper the brief gives as its example.

The trade-off is real and is stated in [limitations](#known-limitations): this is against
LinkedIn's User Agreement, which is why it belongs in a hiring exercise run with a burner
account at hand-scale volume. A production system needing third-party profile data would
license it from a data vendor rather than run this.

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

### Pagination, which is easy to miss

Every section response carries paging metadata:

```jsonc
"paging": { "count": 20, "start": 0, "total": 2 }
```

`count: 20` is the page size, and `total` is the real number of entries. Reading only the
first page returns a **complete-looking** answer that silently drops everything past the
twentieth skill or position — the worst class of bug, because nothing in the response
looks wrong. Sections are therefore paged to exhaustion, capped at five pages, and the cap
is reported in `_meta.warnings` if it is ever hit rather than applied quietly.

`paging.total` also makes empty sections explicable. `total: 0` means the profile genuinely
has no entries, so no warning is emitted; an empty section with no total means it was not
visible to this session, which is worth saying. That distinction is why an empty `skills`
array can be trusted.

### Looking like the client LinkedIn expects

Voyager's own front end identifies itself on every call, and sending nothing where it sends
something is free evidence of not being a browser. Requests carry `x-li-track` (client
version, timezone, form factor), a per-request `x-li-page-instance`, and the
fetch-metadata and client-hint headers Chrome sends. The user-agent and `sec-ch-ua` are
pinned to the same Chrome version, because claiming one version in the UA and another in
the client hints is a worse signal than sending neither.

This is measurable, not superstition. With only `li_at` + `JSESSIONID` and a
three-at-a-time section burst, a live session was revoked mid-fetch and five of nine
sections were lost. With the full cookie jar, serial pacing and these headers, the same
account fetched nine of nine sections with no failures.

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
are all reachable through neighbouring Voyager resources but were not part of the brief.

There is deliberately **no browser fallback**. Beyond being out of scope, it would defeat
the point: the value of hitting the endpoints directly is that ten JSON requests cost a
fraction of what rendering a page costs, and the response is already structured data. If
LinkedIn ever withdraws the `dash` resources, the answer within these constraints is to
find their replacement — `/debug/types` and `capture.py` exist for exactly that — not to
start rendering pages.

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

[Render](https://dashboard.render.com/), from this repo's `Dockerfile` and
[`render.yaml`](render.yaml) — no CLI needed:

1. **New → Blueprint**, connect this GitHub repo. Render reads `render.yaml`.
2. It prompts for the three secrets, because each is declared `sync: false` rather than
   given a value in the file. Paste them there — they live only in the dashboard:

   | Variable | Where it comes from |
   | --- | --- |
   | `LI_AT` | the `li_at` cookie from a logged-in linkedin.com session |
   | `LI_JSESSIONID` | the `JSESSIONID` cookie from that same session |
   | `API_KEY` | any secret you choose; callers send it as `X-API-Key` |

3. Deploy. Render serves it over HTTPS at `https://<service>.onrender.com` and health-checks
   `/healthz`.

To change a cookie later: dashboard → the service → Environment → edit → save, which
redeploys. Nothing sensitive ever goes through git or a shell history.

The container binds `0.0.0.0` on Render's injected `$PORT` and runs as a non-root user.

**Free-tier caveat:** the instance spins down after inactivity, so the first request after
an idle period takes roughly 50 seconds while it wakes. Subsequent requests are normal.
`/healthz` is the cheapest way to wake it before a demo.

### Secrets

Nothing sensitive is in this repository: `.env` and `captures/` are gitignored, `.env.example`
carries only empty keys, and the test suite needs no credentials. Every secret is supplied
as an environment variable at runtime.

---

## License

MIT. Built as a hiring-challenge exercise.
