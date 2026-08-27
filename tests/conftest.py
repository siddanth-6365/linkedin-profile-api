import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "voyager_profile.json"


@pytest.fixture
def payloads() -> tuple[dict, dict]:
    """(primary, sections) as Voyager would return them."""
    data = json.loads(FIXTURE.read_text())
    return data["primary"], data["sections"]


@pytest.fixture
def profile(payloads) -> dict:
    from linkedin_profile_api import normalize

    primary, sections = payloads
    return normalize.normalize(
        "adalovelace", "https://www.linkedin.com/in/adalovelace/", primary, sections
    )
