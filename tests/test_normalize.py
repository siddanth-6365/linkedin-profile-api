"""The normalizer is where a LinkedIn schema change shows up first, so this is
the suite that matters. It asserts behaviour that must survive field renames:
ordering, URN dereferencing, image URL assembly, and honest reporting of gaps.
"""

from linkedin_profile_api import normalize


def test_core_identity(profile):
    core = profile["profile"]
    assert profile["public_id"] == "adalovelace"
    assert profile["urn"] == "urn:li:fsd_profile:ACoAAAtest01"
    assert core["full_name"] == "Ada Lovelace"
    assert core["headline"] == "Mathematician | Analytical Engine notes"
    assert core["about"].startswith("Wrote the first algorithm")


def test_location_and_industry_come_from_dereferenced_urns(profile):
    """geoLocation and industry are URN references, not inline strings."""
    core = profile["profile"]
    assert core["location"]["name"] == "London, England, United Kingdom"
    assert core["location"]["country"] == "GB"
    assert core["location"]["postal_code"] == "SW1A 1AA"
    assert core["industry"] == "Software Development"


def test_enum_urns_become_readable(profile):
    assert profile["profile"]["pronouns"] == "She Her"
    assert profile["experience"][0]["employment_type"] == "Full Time"
    assert profile["languages"][0]["proficiency"] == "Native Or Bilingual"
    assert profile["volunteer"][0]["cause"] == "Science And Technology"


def test_images_are_absolute_and_largest_first(profile):
    """rootUrl + artifact segment, with the biggest artifact as the headline URL."""
    picture = profile["profile"]["images"]["profile_picture"]
    assert picture["url"] == "https://media.licdn.com/dms/image/v2/PROFILE/photo_800.jpg"
    assert len(picture["sizes"]) == 3
    # Sorted ascending regardless of the order LinkedIn listed them in.
    assert [size["width"] for size in picture["sizes"]] == [100, 400, 800]
    assert profile["profile"]["images"]["background"]["url"].endswith("cover_1400.jpg")


def test_open_to_work_inferred_from_photo_frame(profile):
    assert profile["profile"]["open_to_work"] is True


def test_experience_keeps_linkedin_order_and_resolves_company(profile):
    first, second = profile["experience"]
    assert first["title"] == "Principal Mathematician"
    assert second["title"] == "Correspondent"
    assert first["company"] == "Analytical Engine Works"
    assert first["company_url"].endswith("/company/analytical-engine-works/")
    assert first["company_logo"]["url"].endswith("logo_200.png")


def test_section_entity_wins_over_truncated_stub(profile):
    """The primary call returns a bare {title: "Analyst"} stub for position 1.

    The section call returns the same URN in full, and the richer one must win —
    otherwise every profile silently loses the detail it came for.
    """
    assert profile["experience"][0]["title"] == "Principal Mathematician"
    assert "description" in profile["experience"][0]


def test_open_ended_range_is_current_with_a_duration(profile):
    current = profile["experience"][0]
    assert current["current"] is True
    assert "end" not in current
    assert current["duration_months"] > 60  # started 2020-06, still running


def test_closed_range_is_not_current(profile):
    past = profile["experience"][1]
    assert past.get("current") is False
    assert past["end"]["text"] == "2020-05"
    assert past["duration_months"] == 53


def test_partial_dates_keep_their_precision(profile):
    """LinkedIn gives no day for a position; we must not invent one."""
    start = profile["experience"][0]["start"]
    assert start == {"year": 2020, "month": 6, "day": None, "text": "2020-06"}
    # Education is year-only, so the text must stay year-only too.
    assert profile["education"][0]["start"]["text"] == "2012"


def test_education_certifications_honors(profile):
    education = profile["education"][0]
    assert education["school"] == "University of London"
    assert (education["degree"], education["field_of_study"]) == ("BSc", "Mathematics")
    certification = profile["certifications"][0]
    assert certification["authority"] == "Institute of Computation"
    assert certification["license_number"] == "PC-1843"
    assert profile["honors"][0]["date"]["text"] == "2022-11"


def test_skills_and_absent_endorsement_count(profile):
    """An absent count is omitted rather than reported as zero endorsements."""
    algorithms, writing = profile["skills"]
    assert algorithms == {"name": "Algorithms", "endorsement_count": 99}
    assert writing == {"name": "Technical Writing"}


def test_languages_survive_a_missing_elements_list(profile):
    """The `languages` fixture has no `*elements`, which is what a decoration
    change looks like. The fallback reads the included entities directly."""
    assert [language["name"] for language in profile["languages"]] == ["English", "French"]


def test_empty_sections_are_reported_not_hidden(profile):
    assert profile["projects"] == []
    assert profile["_meta"]["section_counts"]["projects"] == 0
    assert any("projects" in warning for warning in profile["_meta"]["warnings"])


def test_meta_counts_every_section(profile):
    counts = profile["_meta"]["section_counts"]
    assert counts["experience"] == 2
    assert set(counts) == set(normalize.MAPPERS)


def test_fetch_failure_warning_is_carried_through(payloads):
    """A section that failed to fetch must be distinguishable from an empty one."""
    primary, sections = payloads
    sections = {key: value for key, value in sections.items() if key != "skills"}
    result = normalize.normalize(
        "adalovelace", "u", primary, sections, ["skills: not fetched — HTTP 429"]
    )
    assert result["skills"] == []
    assert "skills" not in result["_meta"]["section_counts"]
    assert any("not fetched" in warning for warning in result["_meta"]["warnings"])


def test_missing_profile_entity_raises(payloads):
    _, sections = payloads
    try:
        normalize.normalize("nobody", "u", {"data": {}, "included": []}, sections)
    except normalize.ProfileNotFound:
        return
    raise AssertionError("expected ProfileNotFound")


def test_no_null_noise(profile):
    """Absent fields are dropped, so a consumer can trust key presence."""
    assert all(value is not None for value in profile["experience"][1].values())
    assert "grade" not in profile["experience"][0]
