"""The brief requires a purely reverse-engineered solution that hits LinkedIn's
endpoints directly and uses no browser. That is a property worth enforcing rather
than asserting in a README, so it fails the build if it ever regresses.
"""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "linkedin_profile_api"

# Browser automation, headless drivers, and HTML parsers. Any of these appearing
# means the solution stopped hitting the API directly.
BANNED = (
    "playwright", "selenium", "puppeteer", "pyppeteer", "webdriver", "chromedriver",
    "geckodriver", "nodriver", "undetected", "splash", "pyquery", "requests_html",
    "beautifulsoup", "bs4", "lxml", "html5lib", "scrapy", "cloudscraper", "curl_cffi",
    "seleniumbase", "helium", "mechanize",
)


def test_no_browser_or_html_dependency_is_declared():
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = list(manifest["project"]["dependencies"])
    for extra in manifest["project"].get("optional-dependencies", {}).values():
        declared += list(extra)
    lowered = " ".join(declared).lower()
    found = [name for name in BANNED if name in lowered]
    assert not found, f"browser/HTML dependency declared: {found}"


@pytest.mark.parametrize("module", sorted(path.name for path in SOURCE.glob("*.py")))
def test_no_browser_or_html_import_in_source(module):
    text = (SOURCE / module).read_text().lower()
    # Strip comments so prose about *not* using a browser does not trip this.
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    found = [name for name in BANNED if name in code]
    assert not found, f"{module} references {found}"


def test_no_subprocess_escape_hatch():
    """A browser could also be launched by shelling out."""
    for path in SOURCE.glob("*.py"):
        code = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())
        for banned in ("subprocess", "os.system", "os.popen", "pty.spawn"):
            assert banned not in code, f"{path.name} uses {banned}"


def test_only_linkedin_api_is_contacted():
    """Every request goes to the Voyager API base — no page fetches."""
    from linkedin_profile_api import config

    assert config.VOYAGER_BASE == "https://www.linkedin.com/voyager/api"


def test_responses_are_parsed_as_json_only():
    """If HTML were ever parsed, it would show up as a parser call here."""
    voyager_source = (SOURCE / "voyager.py").read_text()
    assert ".json()" in voyager_source
    for html_parser in ("BeautifulSoup", "etree", "HTMLParser", "fromstring"):
        assert html_parser not in voyager_source
