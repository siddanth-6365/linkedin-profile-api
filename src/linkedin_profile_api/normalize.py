"""Voyager's normalized payloads -> our response schema.

Voyager, asked for `application/vnd.linkedin.normalized+json+2.1`, answers with

    {"data": {"*elements": ["urn:...", ...]}, "included": [{entityUrn, $type, ...}]}

— a *flat* entity table plus a list of URN references giving order. So this
module never walks a JSON path. It merges every response's `included` into one
urn -> entity index, then reads each section through that response's own
`*elements` list.

Two things fall out of that. Ordering is LinkedIn's own (experience really is
newest-first). And the primary call and the nine section calls normalize through
exactly the same code, because a merged index does not care which endpoint
supplied an entity. Nested references — position -> company -> logo — resolve
through the same index.

Field *names* inside an entity are still LinkedIn's, and those do drift, so
every read goes through `pick()` with a few candidate spellings.
"""

from datetime import UTC, datetime

SUPPORT_URN_PREFIXES = (
    # Entities that exist to be referenced by others, never as section items.
    "urn:li:fsd_company:",
    "urn:li:fsd_school:",
    "urn:li:fsd_industry:",
    "urn:li:fsd_geo:",
    "urn:li:fsd_organization:",
    "urn:li:fsd_profile:",
)


# --- index -------------------------------------------------------------------

def merge_index(payloads) -> dict[str, dict]:
    """Every `included` entity from every payload, keyed by entityUrn.

    On a collision the richer entity wins: the primary profile call returns
    trimmed stubs of the same positions the section calls return in full.
    """
    index: dict[str, dict] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for entity in payload.get("included") or []:
            if not isinstance(entity, dict):
                continue
            urn = entity.get("entityUrn")
            if urn and len(entity) >= len(index.get(urn, {})):
                index[urn] = entity
    return index


def root_elements(payload) -> list:
    """The `*elements` URN list a finder response uses to convey order."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    holders = [data]
    if isinstance(data.get("data"), dict):
        holders.append(data["data"])
    for holder in holders:
        for key in ("*elements", "elements"):
            value = holder.get(key)
            if isinstance(value, list):
                return value
    return []


def resolve(elements, index: dict[str, dict]) -> list[dict]:
    """URN references (or inlined dicts) -> entities, order preserved."""
    out = []
    for element in elements:
        if isinstance(element, str):
            entity = index.get(element)
            if entity:
                out.append(entity)
        elif isinstance(element, dict):
            out.append(element)
    return out


def section_entities(payload, index: dict[str, dict]) -> list[dict]:
    """Items of a single section response, in LinkedIn's order.

    Falls back to "everything in this response that isn't a support entity" when
    `*elements` is missing, which is the shape a decoration change would take.
    """
    items = resolve(root_elements(payload), index)
    if items:
        return items
    return [
        entity
        for entity in (payload.get("included") or [])
        if isinstance(entity, dict)
        and not str(entity.get("entityUrn", "")).startswith(SUPPORT_URN_PREFIXES)
    ]


def find_profile(payload, index: dict[str, dict]) -> dict | None:
    """The viewee's own Profile entity."""
    for entity in resolve(root_elements(payload), index) + list(index.values()):
        urn = str(entity.get("entityUrn", ""))
        if urn.startswith("urn:li:fsd_profile:") and (
            "firstName" in entity or "publicIdentifier" in entity
        ):
            return entity
    return None


# --- field helpers -----------------------------------------------------------

def pick(entity, *keys):
    """First key present and non-empty. LinkedIn renames fields; we shrug.

    Strings are stripped here because this is the one place every field read
    passes through, and LinkedIn's own data is not clean — Sundar Pichai's
    profile genuinely stores `firstName: "Sundar "`, trailing space included.
    A value that is only whitespace counts as absent.
    """
    if not isinstance(entity, dict):
        return None
    for key in keys:
        value = entity.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, "", [], {}):
            return value
    return None


def deref(entity, index, *keys) -> dict:
    """Follow a URN-valued field (`*company`, `companyUrn`) to its entity."""
    value = pick(entity, *keys)
    if isinstance(value, str):
        return index.get(value) or {}
    if isinstance(value, dict):
        return value
    return {}


def enum_label(value):
    """`urn:li:fsd_employmentType:FULL_TIME` -> `Full Time`."""
    if not isinstance(value, str):
        return None
    tail = value.rsplit(":", 1)[-1]
    if tail.isupper() or "_" in tail:
        return tail.replace("_", " ").title()
    return tail or None


def find_vector_image(node, depth: int = 4) -> dict | None:
    """Locate a vectorImage anywhere in a picture wrapper.

    LinkedIn nests these differently per entity — `profilePicture
    .displayImageReference.vectorImage`, `logo.vectorImage`, sometimes bare — so
    searching for the shape beats hard-coding each path.
    """
    if depth < 0 or not isinstance(node, dict):
        return None
    if "artifacts" in node and "rootUrl" in node:
        return node
    for value in node.values():
        if isinstance(value, dict):
            found = find_vector_image(value, depth - 1)
            if found:
                return found
    return None


def image(node) -> dict | None:
    """vectorImage -> absolute URLs. `rootUrl` + each artifact's path segment."""
    vector = find_vector_image(node)
    if not vector:
        return None
    root = vector.get("rootUrl") or ""
    sizes = []
    for artifact in vector.get("artifacts") or []:
        segment = artifact.get("fileIdentifyingUrlPathSegment")
        if not segment:
            continue
        sizes.append(
            {
                "width": artifact.get("width"),
                "height": artifact.get("height"),
                "url": root + segment,
            }
        )
    if not sizes:
        return None
    sizes.sort(key=lambda size: size["width"] or 0)
    return {"url": sizes[-1]["url"], "sizes": sizes}


def _date(node) -> dict | None:
    """A LinkedIn partial date. No day is invented — LinkedIn rarely has one."""
    if not isinstance(node, dict):
        return None
    year, month, day = node.get("year"), node.get("month"), node.get("day")
    if not year:
        return None
    text = f"{year:04d}"
    if month:
        text += f"-{month:02d}"
        if day:
            text += f"-{day:02d}"
    return {"year": year, "month": month, "day": day, "text": text}


def date_range(entity) -> tuple[dict | None, dict | None]:
    node = pick(entity, "dateRange", "timePeriod") or {}
    start = _date(pick(node, "start", "startDate")) or _date(pick(entity, "issueDate", "date"))
    end = _date(pick(node, "end", "endDate"))
    return start, end


def duration_months(start, end) -> int | None:
    """Inclusive month count; an open-ended range runs to today."""
    if not start:
        return None
    if end:
        end_year, end_month = end["year"], end["month"] or 12
    else:
        now = datetime.now(UTC)
        end_year, end_month = now.year, now.month
    months = (end_year - start["year"]) * 12 + (end_month - (start["month"] or 1)) + 1
    return months if months > 0 else None


def _compact(row: dict) -> dict:
    """Drop keys we found nothing for, so absent != null-noise."""
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


# --- section mappers ---------------------------------------------------------
# Each takes (entity, index) and returns one row. Candidate spellings cover the
# dash field names and their older equivalents.

def _experience(entity, index) -> dict:
    company = deref(entity, index, "*company", "companyUrn", "company")
    start, end = date_range(entity)
    return _compact(
        {
            "title": pick(entity, "title", "name"),
            "company": pick(entity, "companyName") or pick(company, "name"),
            "company_urn": pick(entity, "*company", "companyUrn"),
            "company_url": pick(company, "url"),
            "company_logo": image(pick(company, "logo", "logoResolutionResult") or {}),
            "employment_type": enum_label(
                pick(entity, "employmentTypeUrn", "employmentType")
            ),
            "location": pick(entity, "locationName", "geoLocationName", "location"),
            "description": pick(entity, "description"),
            "start": start,
            "end": end,
            "current": end is None and start is not None,
            "duration_months": duration_months(start, end),
        }
    )


def _education(entity, index) -> dict:
    school = deref(entity, index, "*school", "schoolUrn", "school")
    start, end = date_range(entity)
    return _compact(
        {
            "school": pick(entity, "schoolName") or pick(school, "name"),
            "school_urn": pick(entity, "*school", "schoolUrn"),
            "school_logo": image(pick(school, "logo", "logoResolutionResult") or {}),
            "degree": pick(entity, "degreeName", "degree"),
            "field_of_study": pick(entity, "fieldOfStudy"),
            "grade": pick(entity, "grade"),
            "activities": pick(entity, "activities"),
            "description": pick(entity, "description"),
            "start": start,
            "end": end,
        }
    )


def _skill(entity, _index) -> dict:
    return _compact(
        {
            "name": pick(entity, "name", "skillName"),
            "endorsement_count": pick(
                entity, "endorsementCount", "numEndorsements", "endorsedCount"
            ),
        }
    )


def _certification(entity, index) -> dict:
    authority = deref(entity, index, "*company", "companyUrn", "company")
    start, end = date_range(entity)
    return _compact(
        {
            "name": pick(entity, "name", "title"),
            "authority": pick(entity, "authority") or pick(authority, "name"),
            "authority_logo": image(pick(authority, "logo", "logoResolutionResult") or {}),
            "license_number": pick(entity, "licenseNumber"),
            "url": pick(entity, "url"),
            "start": start,
            "end": end,
        }
    )


def _language(entity, _index) -> dict:
    return _compact(
        {
            "name": pick(entity, "name"),
            "proficiency": enum_label(pick(entity, "proficiency")),
        }
    )


def _project(entity, _index) -> dict:
    start, end = date_range(entity)
    return _compact(
        {
            "title": pick(entity, "title", "name"),
            "description": pick(entity, "description"),
            "url": pick(entity, "url"),
            "start": start,
            "end": end,
        }
    )


def _honor(entity, _index) -> dict:
    start, _ = date_range(entity)
    return _compact(
        {
            "title": pick(entity, "title", "name"),
            "issuer": pick(entity, "issuer"),
            "description": pick(entity, "description"),
            "date": start,
        }
    )


def _volunteer(entity, index) -> dict:
    company = deref(entity, index, "*company", "companyUrn", "company")
    start, end = date_range(entity)
    return _compact(
        {
            "role": pick(entity, "role", "title"),
            "organization": pick(entity, "companyName") or pick(company, "name"),
            "cause": enum_label(pick(entity, "cause")),
            "description": pick(entity, "description"),
            "start": start,
            "end": end,
        }
    )


def _publication(entity, _index) -> dict:
    start, _ = date_range(entity)
    return _compact(
        {
            "name": pick(entity, "name", "title"),
            "publisher": pick(entity, "publisher"),
            "description": pick(entity, "description"),
            "url": pick(entity, "url"),
            "date": start,
        }
    )


MAPPERS = {
    "experience": _experience,
    "education": _education,
    "skills": _skill,
    "certifications": _certification,
    "languages": _language,
    "projects": _project,
    "honors": _honor,
    "volunteer": _volunteer,
    "publications": _publication,
}


# --- core profile ------------------------------------------------------------

def _core(profile: dict, index: dict[str, dict]) -> dict:
    geo = deref(profile, index, "*geoLocation", "geoLocationUrn")
    if not geo:
        geo = deref(pick(profile, "geoLocation") or {}, index, "*geo", "geoUrn")
    industry = deref(profile, index, "*industry", "industryUrn")
    location = pick(profile, "location") or {}
    first = pick(profile, "firstName") or ""
    last = pick(profile, "lastName") or ""
    full_name = " ".join(part for part in (first, last) if part)
    return _compact(
        {
            "first_name": first or None,
            "last_name": last or None,
            "full_name": full_name or None,
            "headline": pick(profile, "headline", "occupation"),
            "about": pick(profile, "summary", "about"),
            "location": _compact(
                {
                    "name": pick(geo, "defaultLocalizedName", "name")
                    or pick(profile, "geoLocationName", "locationName"),
                    "country": (pick(location, "countryCode") or "").upper() or None,
                    "postal_code": pick(location, "postalCode"),
                }
            ),
            "industry": pick(industry, "name") or pick(profile, "industryName"),
            "pronouns": pick(profile, "customPronoun") or enum_label(pick(profile, "pronoun")),
            "premium": pick(profile, "premium", "showPremiumSubscriberBadge"),
            "influencer": pick(profile, "influencer"),
            "open_to_work": pick(profile, "openToWork")
            or (pick(pick(profile, "profilePicture") or {}, "frameType") == "OPEN_TO_WORK")
            or None,
            "follower_count": pick(profile, "followerCount"),
            "connection_count": pick(profile, "connectionCount", "connections"),
            "images": _compact(
                {
                    "profile_picture": image(pick(profile, "profilePicture") or {}),
                    "background": image(pick(profile, "backgroundPicture") or {}),
                }
            ),
        }
    )


def normalize(
    slug: str,
    url: str,
    primary: dict,
    sections: dict[str, dict],
    warnings: list[str] | None = None,
) -> dict:
    """Assemble the response. `sections` maps our key -> that section's payload."""
    warnings = list(warnings or [])
    index = merge_index([primary, *sections.values()])
    profile = find_profile(primary, index)
    if profile is None:
        raise ProfileNotFound(f"No profile entity in LinkedIn's response for {slug!r}.")

    result = {
        "url": url,
        "public_id": pick(profile, "publicIdentifier") or slug,
        "urn": profile.get("entityUrn"),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile": _core(profile, index),
    }

    counts = {}
    for key, mapper in MAPPERS.items():
        payload = sections.get(key)
        if payload is None:
            result[key] = []
            continue
        rows = [mapper(entity, index) for entity in section_entities(payload, index)]
        rows = [row for row in rows if row]
        result[key] = rows
        counts[key] = len(rows)

        # LinkedIn reports the true size in paging.total, so an empty or short
        # section can be explained precisely rather than guessed at.
        total = ((payload.get("data") or {}).get("paging") or {}).get("total")
        if not rows and total == 0:
            continue  # genuinely empty; nothing to warn about
        if not rows:
            warnings.append(
                f"{key}: LinkedIn returned no entries and no total — the section may not "
                "be visible to this account."
            )
        elif isinstance(total, int) and len(rows) < total:
            warnings.append(f"{key}: parsed {len(rows)} of {total} entries LinkedIn reported.")

    result["_meta"] = {
        "source": "linkedin.voyager.identity.dash",
        "section_counts": counts,
        "warnings": warnings,
    }
    return result


class ProfileNotFound(LookupError):
    """LinkedIn answered, but with no profile in it."""
