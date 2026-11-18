"""A learned board's refusals and empty pages, in the reads that run it (docs/PRD.md 37.17, `sites/refusals.py`): a
request, an html and a page recipe each tell a page that says there is nothing from a refusal, rest on a 429 or 503 and
end the run after three in a row, skip a search a sign-in page took and stop the host on a bot check, and the night
never counts an empty page as a bad night nor a refusal as an empty page. A learned table can only make the read more
careful. Every site, page and word is made up (`example.test`); `Scripted` is the polite fetcher and `FakeChrome` the
app's Chrome, so nothing reaches the network or a real browser."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import host_stops, polite
from app.jobsearch.models import JobPosting
from app.jobsearch.postings import site_reads
from app.sites import recipes, refusals, search_fill, skills
from app.sites.models import Site, SiteRecipe, SiteSkill
from tests.builders import browser_world
from tests.jobsearch.test_html_recipes import HOST, NOW, START, _live_site, _parsed, _recipe, listing, shell
from tests.jobsearch.test_page_recipes import RECIPE as PAGE_RECIPE
from tests.jobsearch.test_page_recipes import FakeChrome, row
from tests.support import OWNER

EMPTY_PAGE = "<html><body><ul class='results'></ul><p>No results found for your search.</p></body></html>"
SIGNIN_PAGE = "<html><body><h1>Please sign in to continue</h1></body></html>"
CHECK = "<html><body>Just a moment... verify you are human</body></html>"
FIND = f"https://{HOST}/find?q={{phrase}}"
API = f"https://{HOST}/api/jobs?q={{phrase}}"
SEARCH = search_fill.Search(("Chef", "Sous"), ())
CHEF, SOUS = f"https://{HOST}/find?q=Chef", f"https://{HOST}/find?q=Sous"


class Scripted:
    """The polite fetcher, scripted: `answers` maps an address to what it answers in turn, as (status, text) or (status,
    text, final address); the last answer is repeated. It notes what it was asked and how it was slowed."""

    def __init__(self, answers: dict[str, list[tuple]]) -> None:
        self.answers, self.got, self.requests, self.deadline = {k: list(v) for k, v in answers.items()}, [], 0, None
        self.slowed: list[tuple[str, float, float]] = []
        self.refused_hosts: set[str] = set()

    def slow_host(self, host: str, factor: float, most: float) -> float:
        self.slowed.append((host, factor, most))
        return most

    async def allows(self, url: str) -> bool:
        return True

    async def request(self, method: str, url: str, **_kw) -> httpx.Response:
        self.got.append(url)
        self.requests += 1
        turns = self.answers.get(url)
        if not turns:
            return httpx.Response(404, text="", request=httpx.Request(method, url))
        status, text, *final = turns.pop(0) if len(turns) > 1 else turns[0]
        return httpx.Response(status, text=text, request=httpx.Request(method, final[0] if final else url))

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def no_wait(_seconds):
        return None

    async def allowed(self, url):
        return True

    monkeypatch.setattr(browser_code, "sleep", no_wait)
    monkeypatch.setattr(page_recipe, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(page_recipe, "FIND_SECONDS", 0.0)
    monkeypatch.setattr(recipe_record, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(polite.Fetcher, "allows", allowed)


@pytest.fixture
def stops(monkeypatch):
    """What the read told the host-stop record: (host, status)."""
    told: list[tuple[str, int]] = []

    async def record(host: str, status: int) -> None:
        told.append((host, status))

    monkeypatch.setattr(host_stops, "record_fetch_refusal", record)
    return told


def request_recipe(**over) -> skills.RequestRecipe:
    got = skills.parse_recipe({"kind": "request", "method": "GET", "url": API, "items": "results", "url_prefix": f"https://{HOST}",
                               "fields": {"title": "name", "url": "path", "ext_id": "id"}, **over})
    assert isinstance(got, skills.RequestRecipe)
    return got


def answer(*names: str) -> str:
    return json.dumps({"results": [{"id": n, "name": f"Role {n}", "path": f"/job/{n}"} for n in names]})


# --- an html recipe -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 503])
async def test_html_a_rest_slows_the_host_and_asks_again_and_a_good_page_after_it_is_read(stops, status):
    fetch = Scripted({START: [(status, "slow down"), (200, listing([1, 2]))]})
    got = await html_recipe.run(fetch, _parsed())
    assert [p["title"] for p in got.postings] == ["Programs Director 1", "Programs Director 2"]
    assert fetch.got == [START, START] and len(fetch.slowed) == 1 and fetch.slowed[0][:2] == (HOST, 2.0)
    assert stops == [], "a rest stops no host"


@pytest.mark.parametrize("status", [429, 503])
async def test_html_three_rests_in_a_row_end_the_run_and_the_site_rests_not_a_bad_night(stops, status):
    fetch = Scripted({START: [(status, "slow down")]})
    with pytest.raises(skills.SkillError) as end:
        await html_recipe.run(fetch, _parsed())
    assert end.value.status == status and fetch.got == [START] * 3 and len(fetch.slowed) == 2
    assert recipes.blocked(end.value) is True and stops == []


async def test_html_a_refusal_stops_the_host_until_the_next_off_hours(stops):
    with pytest.raises(skills.SkillError) as stop:
        await html_recipe.run(Scripted({START: [(403, "no")]}), _parsed())
    assert stop.value.status == 403 and stops == [(HOST, 403)]


async def test_html_a_bot_check_stops_the_host_and_is_never_an_empty_page(stops):
    with pytest.raises(skills.SkillError) as stop:
        await html_recipe.run(Scripted({START: [(200, CHECK + " No results found")]}), _parsed())
    assert stop.value.status == 403 and stops == [(HOST, 403)]


async def test_html_a_page_that_says_there_is_nothing_is_an_empty_read_not_a_failure(stops):
    got = await html_recipe.run(Scripted({START: [(200, EMPTY_PAGE)]}), _parsed())
    assert got.postings == [] and refusals.said(got) is True


async def test_html_a_page_that_says_nothing_is_still_a_read_of_no_postings_and_not_said_empty(stops):
    got = await html_recipe.run(Scripted({START: [(200, shell())]}), _parsed())
    assert got.postings == [] and refusals.said(got) is False


async def test_html_a_sign_in_page_is_a_skipped_search_not_an_empty_one_and_the_other_searches_are_read(stops):
    one, two = CHEF, SOUS
    fetch = Scripted({one: [(200, SIGNIN_PAGE)], two: [(200, listing([1, 2]))]})
    got = await html_recipe.run(fetch, _parsed(search_url=FIND), search=SEARCH)
    assert fetch.got == [one, two] and len(got.postings) == 2 and refusals.said(got) is False
    assert got.truncated and "1 searches were sent to a sign-in page" in (got.note or "")


async def test_html_a_redirect_to_a_sign_in_address_skips_the_search_by_its_address(stops):
    one, two = CHEF, SOUS
    fetch = Scripted({one: [(200, shell(), f"https://{HOST}/sso/login?next=x")], two: [(200, listing([3]))]})
    got = await html_recipe.run(fetch, _parsed(search_url=FIND), search=SEARCH)
    assert [p["title"] for p in got.postings] == ["Programs Director 3"]


async def test_html_when_every_search_is_sent_to_a_sign_in_page_the_run_is_a_refusal_not_an_empty_read(stops):
    fetch = Scripted({CHEF: [(200, SIGNIN_PAGE)], SOUS: [(200, SIGNIN_PAGE)]})
    with pytest.raises(skills.SkillError) as end:
        await html_recipe.run(fetch, _parsed(search_url=FIND), search=SEARCH)
    assert end.value.status == 401 and recipes.blocked(end.value) and "sign-in page" in str(end.value)


async def test_html_a_search_that_is_empty_and_one_that_is_not_leave_the_read_with_postings_and_not_said_empty(stops):
    fetch = Scripted({CHEF: [(200, EMPTY_PAGE)], SOUS: [(200, listing([5]))]})
    got = await html_recipe.run(fetch, _parsed(search_url=FIND), search=SEARCH)
    assert len(got.postings) == 1 and refusals.said(got) is False


async def test_html_every_search_empty_is_said_empty_and_still_closes_nothing(stops):
    fetch = Scripted({CHEF: [(200, EMPTY_PAGE)], SOUS: [(200, EMPTY_PAGE)]})
    got = await html_recipe.run(fetch, _parsed(search_url=FIND), search=SEARCH)
    assert got.postings == [] and refusals.said(got) is True and got.truncated


async def test_html_a_learned_word_makes_a_page_empty_and_a_learned_status_cannot_make_a_stop_a_rest(stops):
    page = "<html><body><p>Nothing to show today.</p></body></html>"
    plain = await html_recipe.run(Scripted({START: [(200, page)]}), _parsed())
    taught = await html_recipe.run(Scripted({START: [(200, page)]}),
                                   _parsed(refusals={"empty": ["nothing to show today"]}))
    assert refusals.said(plain) is False and refusals.said(taught) is True
    with pytest.raises(skills.SkillError):
        await html_recipe.run(Scripted({START: [(403, "no")]}), _parsed(refusals={"statuses": {"403": "rest"}}))
    assert stops == [(HOST, 403)]


async def test_html_a_learned_status_makes_the_read_more_careful(stops):
    fetch = Scripted({START: [(451, "gone for legal reasons")]})
    with pytest.raises(skills.SkillError) as stop:
        await html_recipe.run(fetch, _parsed(refusals={"statuses": {"451": "stop"}}))
    assert stop.value.status == 451 and stops == [(HOST, 451)]
    careful = _parsed(refusals={"statuses": {"451": "stop"}})
    loose = await html_recipe.run(Scripted({START: [(200, listing([1]))]}), careful)
    assert len(loose.postings) == 1


# --- a request recipe -----------------------------------------------------------------------------


async def test_request_a_rest_asks_again_and_three_in_a_row_end_the_run(stops):
    chef = f"https://{HOST}/api/jobs?q=Chef"
    got = await skills.run_recipe(Scripted({chef: [(429, ""), (200, answer("a", "b"))]}), request_recipe(),
                                  search=search_fill.Search(("Chef",), ()))
    assert [p["ext_id"] for p in got.postings] == ["a", "b"]
    with pytest.raises(skills.SkillError) as end:
        await skills.run_recipe(Scripted({chef: [(503, "")]}), request_recipe(),
                                search=search_fill.Search(("Chef",), ()))
    assert end.value.status == 503 and recipes.blocked(end.value) is True


async def test_request_a_stop_stops_the_host(stops):
    with pytest.raises(skills.SkillError) as stop:
        await skills.run_recipe(Scripted({f"https://{HOST}/api/jobs?q=Chef": [(401, "")]}), request_recipe(),
                                search=search_fill.Search(("Chef",), ()))
    assert stop.value.status == 401 and stops == [(HOST, 401)]


async def test_request_a_search_answered_with_an_empty_list_is_an_empty_read(stops):
    chef = f"https://{HOST}/api/jobs?q=Chef"
    got = await skills.run_recipe(Scripted({chef: [(200, answer())]}), request_recipe(),
                                  search=search_fill.Search(("Chef",), ()))
    assert got.postings == [] and refusals.said(got) is True and got.truncated


async def test_request_a_list_with_no_search_that_comes_back_empty_is_not_said_to_be_empty(stops):
    plain = request_recipe(url=f"https://{HOST}/api/jobs")
    got = await skills.run_recipe(Scripted({f"https://{HOST}/api/jobs": [(200, answer())]}), plain)
    assert got.postings == [] and refusals.said(got) is False, "a whole board answering none is a changed board"


async def test_request_a_sign_in_page_in_place_of_data_is_a_skipped_search_not_a_changed_shape(stops):
    one, two = f"https://{HOST}/api/jobs?q=Chef", f"https://{HOST}/api/jobs?q=Sous"
    fetch = Scripted({one: [(200, SIGNIN_PAGE)], two: [(200, answer("z"))]})
    got = await skills.run_recipe(fetch, request_recipe(), search=SEARCH)
    assert [p["ext_id"] for p in got.postings] == ["z"] and "sign-in page" in (got.note or "")
    both = Scripted({one: [(200, SIGNIN_PAGE)], two: [(200, SIGNIN_PAGE)]})
    with pytest.raises(skills.SkillError) as end:
        await skills.run_recipe(both, request_recipe(), search=SEARCH)
    assert end.value.status == 401


async def test_request_an_answer_that_is_neither_data_nor_words_the_table_knows_is_still_a_changed_shape(stops):
    fetch = Scripted({f"https://{HOST}/api/jobs?q=Chef": [(200, "<html>welcome</html>")]})
    with pytest.raises(skills.SkillError, match="did not answer with JSON"):
        await skills.run_recipe(fetch, request_recipe(), search=search_fill.Search(("Chef",), ()))


async def test_request_a_json_answer_that_says_please_sign_in_with_no_postings_is_a_skip_not_an_empty_read(stops):
    body = json.dumps({"results": [], "message": "Please sign in to continue"})
    with pytest.raises(skills.SkillError) as end:
        await skills.run_recipe(Scripted({f"https://{HOST}/api/jobs?q=Chef": [(200, body)]}), request_recipe(),
                                search=search_fill.Search(("Chef",), ()))
    assert end.value.status == 401


# --- a page recipe --------------------------------------------------------------------------------


class Said(FakeChrome):
    """`FakeChrome` that answers a look at the page with a page of its own."""

    html = "<html><body><p>raw</p></body></html>"

    async def call(self, method, params=None, session_id=None):
        if "outerHTML" in (params or {}).get("expression", ""):
            return {"result": {"value": json.dumps({"url": self.navigated[-1], "html": self.html})}}
        return await super().call(method, params, session_id)


def chrome(monkeypatch, fake: FakeChrome) -> FakeChrome:
    monkeypatch.setattr(page_recipe, "make_tools", lambda: fake)
    return fake


def page(**over) -> skills.PageRecipe:
    got = skills.parse_recipe({**PAGE_RECIPE, **over})
    assert isinstance(got, skills.PageRecipe)
    return got


async def test_page_a_rest_waits_and_loads_the_page_again_and_three_in_a_row_end_the_run(stops, monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}, status=503))
    with pytest.raises(skills.SkillError) as end:
        await page_recipe.run(page(), ["a"])
    assert end.value.status == 503 and len(fake.navigated) == 3 and recipes.blocked(end.value) is True and stops == []


@pytest.mark.parametrize("status", [401, 403])
async def test_page_a_refusal_stops_the_host(stops, monkeypatch, status):
    chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}, status=status))
    with pytest.raises(skills.SkillError) as stop:
        await page_recipe.run(page(), ["a"])
    assert stop.value.status == status and stops == [("careers.example.test", status)]


async def test_page_a_search_that_shows_a_page_saying_there_is_nothing_is_an_empty_read(stops, monkeypatch):
    fake = chrome(monkeypatch, Said({"a": [[]]}))
    fake.html = EMPTY_PAGE
    got = await page_recipe.run(page(), ["a"])
    assert got.postings == [] and refusals.said(got) is True


async def test_page_a_search_that_shows_a_page_nothing_explains_is_not_said_empty(stops, monkeypatch):
    chrome(monkeypatch, Said({"a": [[]]}))
    got = await page_recipe.run(page(), ["a"])
    assert got.postings == [] and refusals.said(got) is False


async def test_page_a_sign_in_page_with_no_postings_is_a_refusal_never_an_empty_read(stops, monkeypatch):
    fake = chrome(monkeypatch, Said({"a": [[]]}))
    fake.html = SIGNIN_PAGE + " No results found"
    with pytest.raises(skills.SkillError) as end:
        await page_recipe.run(page(), ["a"])
    assert end.value.status == 401 and recipes.blocked(end.value) is True


async def test_page_a_bot_check_with_no_postings_stops_the_host(stops, monkeypatch):
    fake = chrome(monkeypatch, Said({"a": [[]]}))
    fake.html = CHECK
    with pytest.raises(skills.SkillError) as end:
        await page_recipe.run(page(), ["a"])
    assert end.value.status == 403 and stops == [("careers.example.test", 403)]


async def test_page_a_term_with_postings_is_read_whatever_the_page_says(stops, monkeypatch):
    fake = chrome(monkeypatch, Said({"a": [[row(1), row(2)]]}))
    fake.html = SIGNIN_PAGE
    got = await page_recipe.run(page(), ["a"])
    assert len(got.postings) == 2 and refusals.said(got) is False


# --- the night ------------------------------------------------------------------------------------


async def _night(fetch, day: int = 0) -> dict:
    async with db.session_factory()() as session:
        _built_in, reads = await site_reads.plan(session, OWNER, [])
        counts = await site_reads.refresh(session, OWNER, fetch, reads, NOW + timedelta(days=day))
        await session.commit()
    return counts


async def _recipe_row(row_id) -> SiteRecipe:
    async with db.session_factory()() as session:
        return await session.get(SiteRecipe, row_id)  # type: ignore[return-value]


async def test_nights_that_read_a_page_saying_there_is_nothing_are_never_bad_nights_nor_stale(js, stops):
    await browser_world(extra_sites=False)
    live = await _live_site()
    for day in range(3):
        counts = await _night(Scripted({START: [(200, EMPTY_PAGE)]}), day)
        assert counts["failed"] == 0 and counts["faults"] == []
    row_ = await _recipe_row(live.id)
    assert row_.state == "live" and (row_.bad_nights or 0) == 0
    assert [h["why"] for h in row_.health] == ["empty"] * 3 and all(h["rows"] == 0 for h in row_.health)


async def test_nights_that_read_a_page_nothing_explains_are_still_bad_nights_and_make_the_recipe_stale(js, stops):
    await browser_world(extra_sites=False)
    live = await _live_site()
    for day in range(2):
        await _night(Scripted({START: [(200, shell())]}), day)
    assert (await _recipe_row(live.id)).state == "stale"


async def test_nights_a_site_sent_to_a_sign_in_page_or_rested_are_blocked_never_empty_and_never_bad(js, stops):
    await browser_world(extra_sites=False)
    live = await _live_site()
    for day, page_ in enumerate([(200, SIGNIN_PAGE), (503, "busy"), (200, SIGNIN_PAGE)]):
        counts = await _night(Scripted({START: [page_]}), day)
        assert counts["failed"] == 0
    row_ = await _recipe_row(live.id)
    assert row_.state == "live" and (row_.bad_nights or 0) == 0
    assert [h["why"] for h in row_.health] == ["blocked"] * 3


async def test_a_good_night_after_an_empty_one_is_judged_against_the_nights_that_read_roles(js, stops):
    await browser_world(extra_sites=False)
    live = await _live_site()
    await _night(Scripted({START: [(200, listing([1, 2, 3, 4]))]}), 0)
    await _night(Scripted({START: [(200, EMPTY_PAGE)]}), 1)
    counts = await _night(Scripted({START: [(200, listing([1, 2, 3, 4]))]}), 2)
    assert counts["failed"] == 0 and (await _recipe_row(live.id)).state == "live"
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(JobPosting))).all()) == 4


@pytest.mark.parametrize(("listed", "fault"), [(EMPTY_PAGE, False), (shell(), True)])
async def test_a_board_that_had_roles_and_says_there_are_none_is_no_fault_but_one_that_answers_nothing_is(
        js, stops, monkeypatch, listed, fault):
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=START, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        session.add(SiteSkill(owner=OWNER, site_id=site.id, version=1, recipe=_recipe(), created_at=NOW))
        await session.commit()
    await _night(Scripted({START: [(200, listing([1, 2]))]}), 0)

    async def nothing_stored(*_a, **_kw):
        return 0

    monkeypatch.setattr(site_reads.postings, "store", nothing_stored)
    counts = await _night(Scripted({START: [(200, listed)]}), 1)
    assert bool(counts["faults"]) is fault and counts["failed"] == 0

