"""A board read by a code recipe (a data request, an html page, a page read in Chrome) says where a role's own apply or
employer address sits (`apply`, PRD 37.17). The read only reads that address, never opens or clicks it; the existing
follow flow takes the role there. An address back on the board, or on another board, is refused. Every site is made up
(`*.example.test`); nothing here reaches the network, a model or a browser."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.jobsearch.browser.recipes import html_record
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.fetch.polite import Fetcher
from app.sites import added, recipes, registry, skills
from app.sites import html_select as hs
from app.sites.registry import Registry
from app.sites.schema import Runbook
from app.sites.skills import parse_recipe, run_recipe

HOST = "jobs.example.test"
HOME = f"https://{HOST}"
EMPLOYER = "https://careers.employer.example.test/p/"


@pytest.fixture(autouse=True)
def _another_board():
    """`board.test` is a job board the app knows by name: a role whose address leads there is no employer's."""
    board = Runbook.model_validate({"schema": 1, "key": "other_board", "name": "Other Board", "kind": "board",
                                    "hosts": ["board.test"]})
    with registry.replaced(Registry([board])):
        yield


def _fetcher(handle) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if request.url.path == "/robots.txt" else handle(request)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


def _request(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": f"{HOME}/api/search", "items": "jobs", "url_prefix": HOME,
            "fields": {"title": "title", "url": "path", "ext_id": "id"}}
    return {**base, **over}


async def _read_request(rows: list[dict], **over):
    listing = await run_recipe(_fetcher(lambda r: httpx.Response(200, json={"jobs": rows})), _request(**over))
    return {p["ext_id"]: p for p in listing.postings}


# --- a data request: the address is a field of the item ------------------------------------------------------------


async def test_a_request_recipe_reads_the_employers_address_from_a_data_field_and_keeps_the_boards_own_link():
    rows = [{"id": "1", "title": "Role 1", "path": "/job/1", "links": {"apply": f"{EMPLOYER}1"}}]
    got = await _read_request(rows, apply="links.apply")
    assert got["1"]["apply_url"] == f"{EMPLOYER}1" and got["1"]["url"] == f"{HOME}/job/1"


@pytest.mark.parametrize("address", [
    f"{HOME}/apply/1", "/apply/1", "https://www.jobs.example.test/apply/1", "http://careers.employer.example.test/1",
    "https://board.test/jobs/view/1", "not an address", None, 7])
async def test_an_address_back_on_the_board_or_on_another_board_or_not_public_https_is_refused(address):
    rows = [{"id": "1", "title": "Role 1", "path": "/job/1", "apply": address}]
    got = await _read_request(rows, apply="apply")
    assert got["1"]["apply_url"] == f"{HOME}/job/1", "the posting keeps its own address"


async def test_a_request_recipe_with_no_apply_field_follows_nothing():
    rows = [{"id": "1", "title": "Role 1", "path": "/job/1", "apply": f"{EMPLOYER}1"}]
    got = await _read_request(rows)
    assert got["1"]["apply_url"] == got["1"]["url"]


def test_apply_is_a_plain_dotted_path_and_a_page_or_html_recipe_reads_it_inside_a_row_only():
    assert parse_recipe(_request(apply="links.apply")).apply == "links.apply"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="dotted path"):
        parse_recipe(_request(apply="a b/c;"))
    page = {"kind": "page", "url": f"{HOME}/jobs", "links": "a.job", "apply": "a.apply-link"}
    html = {"kind": "html", "url": f"{HOME}/jobs", "links": "a.job", "apply": "a.apply-link"}
    for recipe in (page, html):
        with pytest.raises(ValueError, match="inside a row"):
            parse_recipe(recipe)
        assert parse_recipe({**recipe, "row": "li.row"}).apply == "a.apply-link"  # type: ignore[union-attr]


# --- an html page: the address is an attribute on the card ----------------------------------------------------------


def _cards(*applies: str | None, attr: str = "data-apply-url") -> str:
    rows = "".join(
        f'<li class="row"><a class="job" href="/job/{n}">Programs Director {n}</a>'
        + (f'<a class="apply-link" {attr}="{a}">Apply</a>' if a else "") + "</li>" for n, a in enumerate(applies, 1))
    return f"<html><body><ul>{rows}</ul></body></html>"


HTML = {"kind": "html", "url": f"{HOME}/openings", "links": "a.job", "row": "li.row", "apply": "a.apply-link"}


async def _read_html(page: str, seen: list[str] | None = None, **over):
    def handle(request):
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    recipe = parse_recipe({**HTML, **over})
    assert isinstance(recipe, skills.HtmlRecipe)
    listing = await html_recipe.run(_fetcher(handle), recipe)
    return {p["title"]: p for p in listing.postings}


async def test_an_html_recipe_reads_the_employers_address_from_a_card_attribute_and_never_opens_it():
    seen: list[str] = []
    got = await _read_html(_cards(f"{EMPLOYER}1", f"{EMPLOYER}2"), seen)
    assert sorted(p["apply_url"] for p in got.values()) == [f"{EMPLOYER}1", f"{EMPLOYER}2"]
    assert {p["url"] for p in got.values()} == {f"{HOME}/job/1", f"{HOME}/job/2"}
    assert seen and all(HOST in u for u in seen), "only the board was fetched: the address is read, not opened"


async def test_an_html_card_whose_address_is_the_board_or_another_board_or_missing_keeps_its_own_link():
    page = _cards(f"{HOME}/apply/1", "https://board.test/jobs/view/1", None, f"{EMPLOYER}4")
    got = await _read_html(page)
    assert got["Programs Director 1"]["apply_url"] == f"{HOME}/job/1"
    assert got["Programs Director 2"]["apply_url"] == f"{HOME}/job/2"
    assert got["Programs Director 3"]["apply_url"] == f"{HOME}/job/3"
    assert got["Programs Director 4"]["apply_url"] == f"{EMPLOYER}4"


async def test_an_html_recipe_with_no_apply_selector_follows_nothing():
    got = await _read_html(_cards(f"{EMPLOYER}1", f"{EMPLOYER}2"), apply=None)
    assert all(p["apply_url"] == p["url"] for p in got.values())


def test_the_recording_finds_the_apply_selector_by_code_when_most_cards_carry_an_employer_address():
    root = hs.parse(_cards(f"{EMPLOYER}1", f"{EMPLOYER}2", f"{EMPLOYER}3"))
    assert html_record.row_fields(root, "a.job", "li.row", f"{HOME}/openings").get("apply") == "[data-apply-url]"
    assert "apply" not in html_record.row_fields(root, "a.job", "li.row"), "no address of the page, no apply"
    own = hs.parse(_cards(f"{HOME}/a/1", f"{HOME}/a/2", f"{HOME}/a/3"))
    assert "apply" not in html_record.row_fields(own, "a.job", "li.row", f"{HOME}/openings")
    anchors = hs.parse(_cards(f"{EMPLOYER}1", f"{EMPLOYER}2", attr="href"))
    assert html_record.row_fields(anchors, "a.job", "li.row", f"{HOME}/openings").get("apply") == "a.apply-link"


# --- a page read in Chrome: the address is an attribute on the card -------------------------------------------------


def test_a_page_row_keeps_the_employers_address_the_card_holds_and_drops_one_that_is_not_the_employers():
    row = {"href": f"{HOME}/job/1", "title": "Programs Director", "apply": f"{EMPLOYER}1"}
    kept = page_recipe._kept(row, f"{HOME}/jobs")
    assert kept is not None and kept["apply"] == f"{EMPLOYER}1"
    for bad in (f"{HOME}/apply/1", "https://board.test/jobs/view/1", "javascript:void(0)", None):
        other = page_recipe._kept({**row, "apply": bad}, f"{HOME}/jobs")
        assert other is not None and "apply" not in other
    bare = page_recipe._kept({k: v for k, v in row.items() if k != "apply"}, f"{HOME}/jobs")
    assert bare is not None and "apply" not in bare


def test_the_pages_script_reads_the_card_attribute_and_clicks_nothing():
    script = page_recipe.rows_of("a.job", "li.row", None, None, None, "a.apply-link")
    assert "getAttribute" in script and ".click(" not in script and "data-apply-url" in script


# --- the code recipe's recording: the request recipe's path is found by code ----------------------------------------


def _capture(rows: list[dict]) -> dict:
    return {"responses": [{"method": "GET", "url": f"{HOME}/api/search", "body": json.dumps({"jobs": rows})}]}


def test_the_recording_finds_the_data_path_that_holds_the_employers_address():
    rows = [{"id": str(n), "title": f"R{n}", "path": f"/job/{n}", "meta": {"ext": f"{EMPLOYER}{n}"},
             "self": f"{HOME}/job/{n}"} for n in range(1, 4)]
    assert recipes.find_apply(_request(), _capture(rows)) == "meta.ext"
    assert recipes.find_apply(_request(apply="meta.ext"), _capture(rows)) is None, "already named"
    bare = [{k: v for k, v in r.items() if k != "meta"} for r in rows]
    assert recipes.find_apply(_request(), _capture(bare)) is None
    assert recipes.find_apply(_request(), _capture(rows[:1])) is None, "one item shows no pattern"


# --- the flag: the board's roles are followed ----------------------------------------------------------------------


def _follow_of(recipe: dict | None) -> bool:
    row = SimpleNamespace(id="a", url=f"{HOME}/jobs", added_by="owner", list_name="search", on=True, polite=None)
    plain = Registry([], (), restricted_present=True, terms=())
    return added.entries_of([row], plain, {"a": recipe} if recipe is not None else {})[HOST].follow


def test_a_code_recipe_that_says_where_the_address_sits_switches_the_boards_follow_on_and_none_leaves_it_off():
    page = {"kind": "page", "url": f"{HOME}/jobs", "links": "a.job", "row": "li.row"}
    assert _follow_of(_request(apply="links.apply")) is True
    assert _follow_of({**HTML, "apply": "a.apply-link"}) is True
    assert _follow_of({**page, "apply": "a.apply-link"}) is True
    assert _follow_of(_request()) is False
    assert _follow_of({**HTML, "apply": None}) is False
    assert _follow_of(None) is False
