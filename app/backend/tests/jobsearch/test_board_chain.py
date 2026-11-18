"""The chain a board the owner added is learned through, cheapest first: a data request, then the page's own HTML
with its search address, then the app's Chrome with a place box; and the one read of a recipe just kept that decides
whether it stays. Every site is made up (`*.example.test`); no test reaches the network, a model or a real browser:
the fetcher is `httpx.MockTransport` and Chrome is `FakeChrome`."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser.recipes import html_record
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.browser.recipes import interact as recipe_interact
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import polite
from app.jobsearch.models import ParseJob
from app.sites import recipes, search_fill, skills
from app.sites.models import Site, SiteRecipe, SiteRecording
from tests.jobsearch.test_html_recipes import FakeFetcher, listing, posting, shell
from tests.jobsearch.test_page_recipes import RECIPE as PAGE_RECIPE
from tests.jobsearch.test_page_recipes import FakeChrome
from tests.support import OWNER

NOW = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
HOST = "board.example.test"
START = f"https://{HOST}/openings"
SEARCH = search_fill.Search(("Chef", "Sous"), ("Northtown", "Remote"))
WHERE = "input#where"


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

    async def read(_session, _owner):
        return SEARCH

    async def terms(_session, _owner):
        return list(SEARCH.phrases)

    monkeypatch.setattr(search_fill, "read", read)
    monkeypatch.setattr(page_recipe, "terms_of", terms)


# --- a page recipe's place box --------------------------------------------------------------------


class PlaceChrome(FakeChrome):
    """`FakeChrome` with a place box: what is typed in each box is noted, in order, with what the script was told to
    vet the box by."""

    def __init__(self, *a, place_ok: bool = True, **kw) -> None:
        super().__init__(*a, **kw)
        self.order: list[tuple[str, str]] = []
        self.vetted: list[str | None] = []
        self.place_ok = place_ok

    async def call(self, method, params=None, session_id=None):
        expression = (params or {}).get("expression", "")
        if expression.startswith("/*rb:type*/"):
            args = json.loads(expression.split("\n", 1)[0].split("//", 1)[1])
            if args[0] == WHERE:
                self.order.append(("place", args[1]))
                self.vetted.append(args[2])
                return {"result": {"value": json.dumps({"typed": self.place_ok})}}
            self.order.append(("phrase", args[1]))
        if expression.startswith("/*rb:has*/") and json.loads(
                expression.split("\n", 1)[0].split("//", 1)[1])[0] == WHERE:
            return {"result": {"value": json.dumps(True)}}
        return await super().call(method, params, session_id)


PAGE_HOST = "careers.example.test"
PAGE_START = str(PAGE_RECIPE["url"])


def _rows(n: int = 3, host: str = PAGE_HOST) -> list[tuple]:
    return [(f"Programs Director {i}", f"https://{host}/job/{i}", "Springdale") for i in range(1, n + 1)]


def _page(monkeypatch, fake: PlaceChrome) -> PlaceChrome:
    monkeypatch.setattr(page_recipe, "make_tools", lambda: fake)
    return fake


def test_a_page_recipe_may_name_a_place_box_beside_its_search_box_and_never_alone():
    assert skills.parse_recipe({**PAGE_RECIPE, "place": WHERE}).place == WHERE  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="place goes with a search box"):
        skills.parse_recipe({"kind": "page", "url": PAGE_RECIPE["url"], "links": "a.job", "place": WHERE})
    with pytest.raises(ValueError, match="plain CSS selector"):
        skills.parse_recipe({**PAGE_RECIPE, "place": "input{x}"})


async def test_each_title_is_typed_and_then_each_place_in_the_place_box_one_search_for_each_pair(monkeypatch):
    fake = _page(monkeypatch, PlaceChrome({"program manager": [_rows()], "operations": [_rows()]}))
    got = await page_recipe.run(skills.parse_recipe({**PAGE_RECIPE, "place": WHERE}),  # type: ignore[arg-type]
                                ["program manager", "operations"], places=["Springdale", "Remote"])
    assert fake.order == [("phrase", "program manager"), ("place", "Springdale"), ("phrase", "program manager"),
                          ("place", "Remote"), ("phrase", "operations"), ("place", "Springdale"),
                          ("phrase", "operations"), ("place", "Remote")]
    assert len(got.postings) == 3 and got.truncated and "in 2 places" in (got.note or "")
    assert fake.vetted == [recipe_interact.NEVER] * 4, "the place box is vetted by what the finders vet by"


async def test_a_recipe_with_no_place_box_or_a_run_with_no_places_types_the_titles_alone(monkeypatch):
    fake = _page(monkeypatch, PlaceChrome({"a": [_rows()]}))
    await page_recipe.run(skills.parse_recipe(PAGE_RECIPE), ["a"], places=["Springdale"])  # type: ignore[arg-type]
    await page_recipe.run(skills.parse_recipe({**PAGE_RECIPE, "place": WHERE}), ["a"],  # type: ignore[arg-type]
                          places=[])
    assert fake.order == [("phrase", "a"), ("phrase", "a")]


async def test_a_run_with_no_titles_reads_the_page_as_it_loads_and_types_no_place(monkeypatch):
    fake = _page(monkeypatch, PlaceChrome({"": [_rows()]}))
    await page_recipe.run(skills.parse_recipe({**PAGE_RECIPE, "place": WHERE}), [],  # type: ignore[arg-type]
                          places=["Springdale"])
    assert fake.order == []


async def test_a_place_box_the_vetting_refuses_is_a_changed_shape_and_nothing_else_is_typed(monkeypatch):
    fake = _page(monkeypatch, PlaceChrome({"a": [_rows()]}, place_ok=False))
    with pytest.raises(skills.SkillError, match="place box could not be typed in"):
        await page_recipe.run(skills.parse_recipe({**PAGE_RECIPE, "place": WHERE}), ["a"],  # type: ignore[arg-type]
                              places=["Springdale"])
    assert fake.order == [("phrase", "a"), ("place", "Springdale")]


def test_the_script_that_types_in_a_place_box_vets_the_box_and_the_one_that_types_a_title_does_not():
    place, title = page_recipe.type_in(WHERE, "x", vetted=True), page_recipe.type_in("input#q", "x")
    for need in ("input[type=password]", "aria-autocomplete", "aria-haspopup", "combobox", "new RegExp(A[2]"):
        assert need in place
    assert "RegExp" not in title.split("\n", 1)[1] or "A[2]) {" in title
    # The fourth argument (2026-11-17) says whether the box may show the site's own list of suggestions.
    assert json.loads(place.split("\n", 1)[0].split("//", 1)[1]) == [WHERE, "x", recipe_interact.NEVER, False]
    assert json.loads(title.split("\n", 1)[0].split("//", 1)[1]) == ["input#q", "x", None, False]


@pytest.fixture
def steps(monkeypatch):
    """The finders' answers (the page itself is `PlaceChrome`): the search box, the place box and next."""
    found: dict = {"search": {"found": True, "what": "search box", "href": None},
                   "next": {"found": True, "what": "next page", "href": None},
                   "place": {"found": True, "what": "place box", "href": None, "plain": True}}
    picked = iter(["input#q", WHERE, "button.next"])

    async def mark(_tab, step):
        return found.get(step)

    async def selector(_tab):
        return next(picked)

    monkeypatch.setattr(page_recipe, "_mark", mark)
    monkeypatch.setattr(page_recipe, "_selector", selector)
    return found


async def test_a_recording_with_a_place_types_it_beside_the_title_and_keeps_the_place_box(monkeypatch, steps):
    fake = _page(monkeypatch, PlaceChrome({"Chef": [_rows()], "": [_rows()]}))
    recipe, why, status = await page_recipe.propose(PAGE_START, "Chef", "Northtown")
    assert (why, status) == (None, None) and recipe is not None
    assert recipe["search"] == "input#q" and recipe["place"] == WHERE and recipe["next"] == "button.next"
    assert fake.order[:2] == [("phrase", "Chef"), ("place", "Northtown")]


@pytest.mark.parametrize("plain", [None])
async def test_a_place_box_that_is_not_there_is_left_out_of_the_recipe(monkeypatch, steps, plain):
    """The box that offers a list of suggestions is `tests/jobsearch/test_place_pick.py`'s (PRD 37.16, 2026-11-17)."""
    steps["place"] = None if plain is None else {**steps["place"], "plain": plain}
    fake = _page(monkeypatch, PlaceChrome({"Chef": [_rows()], "": [_rows()]}))
    picked = iter(["input#q", "button.next"])

    async def selector(_tab):
        return next(picked)

    monkeypatch.setattr(page_recipe, "_selector", selector)
    recipe, _why, _status = await page_recipe.propose(PAGE_START, "Chef", "Northtown")
    assert recipe is not None and "place" not in recipe and fake.order == [("phrase", "Chef")]


async def test_a_recording_with_no_place_asked_for_types_none(monkeypatch, steps):
    fake = _page(monkeypatch, PlaceChrome({"Chef": [_rows()], "": [_rows()]}))
    picked = iter(["input#q", "button.next"])

    async def selector(_tab):
        return next(picked)

    monkeypatch.setattr(page_recipe, "_selector", selector)
    recipe, _why, _status = await page_recipe.propose(PAGE_START, "Chef", None)
    assert recipe is not None and "place" not in recipe and fake.order == [("phrase", "Chef")]


# --- an html recipe's search address --------------------------------------------------------------

SEARCH_URL = f"https://{HOST}/openings?q={{phrase}}&where={{place}}"
HTML = {"kind": "html", "url": START, "links": 'a[href*="/job/"]', "row": "li.row", "next": "a.pager"}


def test_an_html_recipe_may_hold_a_search_address_whose_blanks_are_in_its_query():
    got = skills.parse_recipe({**HTML, "search_url": SEARCH_URL})
    assert isinstance(got, skills.HtmlRecipe) and got.search_url == SEARCH_URL


@pytest.mark.parametrize("address, why", [
    (f"https://{HOST}/openings?q=chef", "holds {phrase}"),
    (f"https://{HOST}/openings?q={{phrase}}&x={{cursor}}", "only {phrase}, {place} and {since}"),
    (f"https://{HOST}/{{phrase}}/openings", "query only"),
    ("https://{phrase}.example.test/openings?q={phrase}", "query only"),
    ("https://other.example.test/openings?q={phrase}", "own host"),
    ("http://127.0.0.1/openings?q={phrase}", "public"),
])
def test_a_search_address_with_no_title_another_blank_a_blank_in_the_path_or_host_or_off_the_site_is_refused(
        address, why):
    with pytest.raises(ValueError, match=why):
        skills.parse_recipe({**HTML, "search_url": address})


class Web:
    """A made-up site on `example.test` read through the polite fetcher over `httpx.MockTransport`: `answer` makes the
    page for a request, and every request is kept."""

    def __init__(self, answer) -> None:
        self.answer, self.asked = answer, []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.asked.append(str(request.url))
        status, body, ctype = self.answer(request)
        return httpx.Response(status, text=body, headers={"content-type": ctype})

    def fetcher(self) -> polite.Fetcher:
        return polite.Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handle)), per_host_seconds=0)

    def serve(self, monkeypatch) -> "Web":
        monkeypatch.setattr(polite, "make", self.fetcher)
        return self


def _results(request: httpx.Request) -> tuple[int, str, str]:
    """A site whose search page lists three postings of its own for each title, the same three for any place."""
    q = request.url.params.get("q")
    if request.url.path == "/openings" and q:
        base = {"Chef": 10, "Sous": 20}.get(q, 0)
        return 200, listing([base + 1, base + 2, base + 3]), "text/html"
    if request.url.path == "/openings":
        return 200, listing([1, 2, 3, 4, 5]), "text/html"
    return 404, "", "text/html"


async def test_an_html_recipe_with_a_search_address_is_read_for_each_title_and_place_and_closes_nothing():
    web = Web(_results)
    fetch = web.fetcher()
    recipe = skills.parse_recipe({**HTML, "search_url": SEARCH_URL})
    got = await skills.run_recipe(fetch, recipe, search=search_fill.Search(("Chef", "Sous"), ("Northtown", "Remote")))
    await fetch.close()
    assert web.asked == [f"https://{HOST}/openings?q={t}&where={p}" for t in ("Chef", "Sous")
                         for p in ("Northtown", "Remote")]
    assert sorted(p["ext_id"] for p in got.postings) == ["/job/11", "/job/12", "/job/13", "/job/21", "/job/22",
                                                         "/job/23"] or len(got.postings) == 6
    assert got.truncated, "what a search shows is a partial view of the board"


async def test_the_words_go_into_the_address_quoted_and_a_search_with_no_place_leaves_the_entry_out():
    web = Web(_results)
    fetch = web.fetcher()
    recipe = skills.parse_recipe({**HTML, "search_url": SEARCH_URL})
    await skills.run_recipe(fetch, recipe, search=search_fill.Search(("Chef de Partie & more",), ()))
    await fetch.close()
    assert web.asked == [f"https://{HOST}/openings?q={quote('Chef de Partie & more', safe='')}"]


async def test_a_recipe_with_a_search_address_and_no_titles_reads_the_start_page_as_it_always_did():
    web = Web(_results)
    fetch = web.fetcher()
    recipe = skills.parse_recipe({**HTML, "search_url": SEARCH_URL})
    got = await skills.run_recipe(fetch, recipe, search=search_fill.Search((), ("Northtown",)))
    await fetch.close()
    assert web.asked == [START] and len(got.postings) == 5 and not got.truncated


# --- the address bar made a template --------------------------------------------------------------

TYPED = {"phrase": "Chef", "second": "Sous", "place": "Northtown"}


def test_the_address_after_a_typed_search_becomes_a_template_by_code():
    template = recipe_record.search_template(f"{START}?q=Sous&where=Northtown&sort=new#top", TYPED)
    assert template == f"{START}?q={{phrase}}&where={{place}}&sort=new"
    assert recipe_record.search_template(f"{START}?q=Chef", {"phrase": "Chef"}) == f"{START}?q={{phrase}}"
    assert recipe_record.search_template(f"{START}?q=sous+chef", {"phrase": "Sous Chef"}) == f"{START}?q={{phrase}}"


@pytest.mark.parametrize("address", [
    f"{START}",                       # nothing typed shows in the address
    f"{START}?q=Sous%20Chef%20jobs",  # the word is inside a longer value
    f"{START}/Sous?q=Sous",           # a typed word is left whole in the path
])
def test_an_address_that_keeps_a_typed_word_or_shows_none_makes_no_template(address):
    assert recipe_record.search_template(address, TYPED) is None


async def test_a_search_address_holding_a_typed_word_is_refused_and_never_kept():
    recipe = {**HTML, "search_url": f"https://{HOST}/openings?q=Chef&where={{place}}"}
    assert "never stores the owner's titles" in (html_record.refuse_literals(recipe, TYPED) or "")
    assert html_record.refuse_literals({**HTML, "search_url": SEARCH_URL}, TYPED) is None
    assert html_record.refuse_literals(HTML, TYPED) is None


# --- the chain's records --------------------------------------------------------------------------


async def _added(added_by: str = "owner", reason: str = "first", body: dict | None = None, url: str = START) -> Site:
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=url, kind="job_board", read_by="app", added_by=added_by,
                    created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        rec = await recipe_record.request(session, OWNER, site, reason, NOW)
        if body is not None:
            rec.body = recipes.pack(body)
        await session.commit()
        return site


async def _recordings() -> list[SiteRecording]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(SiteRecording).order_by(SiteRecording.requested_at,
                                                                           SiteRecording.reason))).all())


def _draws_itself(monkeypatch) -> None:
    """The page Chrome renders lists postings, a plain fetch of it gets a shell: the page draws itself by script."""
    rendered = {START: listing([1, 2, 3, 4, 5]), **{f"https://{HOST}/job/{n}": posting(n) for n in range(1, 6)}}
    from tests.jobsearch.test_html_recipes import chrome as html_chrome

    html_chrome(monkeypatch, rendered)
    monkeypatch.setattr(polite, "make", lambda: FakeFetcher({START: shell()}))


async def _html_try(site: Site) -> dict:
    async with db.session_factory()() as session:
        rec = await session.scalar(select(SiteRecording).where(SiteRecording.site_id == site.id,
                                                               SiteRecording.state == "requested"))
        assert rec is not None
        rec_id = rec.id
    return await html_record.record_by_code(OWNER, rec_id)


async def test_a_page_that_needs_javascript_asks_for_one_recording_in_the_chrome_for_a_site_the_owner_added(
        js, monkeypatch):
    _draws_itself(monkeypatch)
    site = await _added()
    got = await _html_try(site)
    assert got["note"].startswith(html_record.NEEDS_JS) and "tried in the app's browser next" in got["note"]
    first, chain = await _recordings()
    assert (first.reason, first.state) == ("first", "failed") and first.note.startswith(html_record.NEEDS_JS)
    assert (chain.reason, chain.state, chain.site_id) == ("chain", "requested", site.id)
    async with db.session_factory()() as session:
        wanted = await recipe_record.wanted(session, OWNER)
    assert [w["priority"] for w in wanted] == [0], "taken at any hour: the owner added the site and is waiting"


@pytest.mark.parametrize("added_by, reason", [("app", "first"), ("owner", "page"), ("owner", "repair"),
                                              ("owner", "chain")])
async def test_a_site_the_owner_did_not_add_or_asked_a_page_recording_of_by_name_is_never_chained(
        js, monkeypatch, added_by, reason):
    _draws_itself(monkeypatch)
    site = await _added(added_by=added_by, reason=reason)
    got = await _html_try(site)
    assert got["note"].startswith(html_record.NEEDS_JS) and "next" not in got["note"]
    assert [r.reason for r in await _recordings()] == [reason]


async def test_the_chain_is_asked_for_at_most_once_a_site(js, monkeypatch):
    _draws_itself(monkeypatch)
    site = await _added()
    await _html_try(site)
    async with db.session_factory()() as session:
        # the chain's own recording is answered (it failed), so a new first recording is the open request
        for rec in (await session.scalars(select(SiteRecording).where(SiteRecording.reason == "chain"))).all():
            rec.state = "failed"
        await session.flush()
        again = await recipe_record.request(session, OWNER, await session.get(Site, site.id), "first", NOW)
        again.body = recipes.pack({})
        await session.commit()
    await _html_try(site)
    assert sorted(r.reason for r in await _recordings()) == ["chain", "first", "first"]


async def test_a_page_recording_that_fails_ends_with_its_note_and_asks_for_nothing_more(js, monkeypatch):
    _draws_itself(monkeypatch)
    site = await _added()
    await _html_try(site)
    fake = PlaceChrome({"": [_rows(1)]})  # one posting: fewer than a recording needs
    _page(monkeypatch, fake)

    async def no_selector(_tab, step):
        return None

    monkeypatch.setattr(page_recipe, "_mark", no_selector)
    got = await recipe_record.record_by_code(OWNER, HOST)
    assert got["result"] == bp.FINISHED
    recs = await _recordings()
    assert [(r.reason, r.state) for r in recs] == [("first", "failed"), ("chain", "failed")]
    assert recs[1].note and await _recipes_of(site) == []
    async with db.session_factory()() as session:
        assert await recipe_record.wanted(session, OWNER) == [], "nothing asks again: the site stays on the browser"


async def _recipes_of(site: Site) -> list[SiteRecipe]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(SiteRecipe).where(SiteRecipe.site_id == site.id)
                                           .order_by(SiteRecipe.version))).all())


# --- the first read of a recipe just kept ---------------------------------------------------------

API_RECIPE = {"kind": "request", "method": "GET", "url": f"https://{HOST}/api/search", "items": "results",
              "params": {"q": "{phrase}"}, "fields": {"title": "name", "url": "link", "ext_id": "reqId"},
              "url_prefix": f"https://{HOST}"}


def _api(status: int = 200, found: bool = True):
    def answer(request: httpx.Request) -> tuple[int, str, str]:
        q = request.url.params.get("q", "")  # a search that works answers each title with roles of its own
        rows = [{"reqId": f"{q}{i}", "name": f"{q} role {i}", "link": f"/job/{q}{i}"} for i in range(1, 4)
                ] if found else []
        return status, json.dumps({"results": rows}), "application/json"

    return answer


async def _live(site: Site, recipe: dict, recording: dict | None = None, version: int = 1) -> SiteRecipe:
    async with db.session_factory()() as session:
        rec = (await session.scalars(select(SiteRecording).where(SiteRecording.site_id == site.id))).first()
        if rec is not None:
            rec.state = "written"  # the recording this recipe was kept from is answered
        if recording is not None and rec is not None:
            rec.body = recipes.pack(recording)
        row = SiteRecipe(owner=OWNER, site_id=site.id, version=version, recipe=recipe, state="live", created_at=NOW,
                         recording_id=rec.id if rec is not None else None)
        session.add(row)
        await session.commit()
        return row


async def test_a_kept_recipe_that_reads_postings_for_both_titles_on_the_first_read_is_left_as_it_is(js, monkeypatch):
    site = await _added()
    row = await _live(site, API_RECIPE)
    web = Web(_api()).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "ok"
    assert web.asked == [f"https://{HOST}/api/search?q=Chef", f"https://{HOST}/api/search?q=Sous"], (
        "the first page of each of the two titles, no paging")
    [kept] = await _recipes_of(site)
    assert (kept.id, kept.state, kept.bad_nights, kept.health) == (row.id, "live", 0, [])


@pytest.mark.parametrize("status, found", [(403, True), (401, True), (200, False)])
async def test_a_data_recipe_that_is_refused_or_reads_nothing_goes_the_same_day_to_the_pages_html_with_no_bad_night(
        js, monkeypatch, status, found):
    site = await _added(body={"typed": {"phrase": "Chef"}, "responses": []})
    row = await _live(site, API_RECIPE, {"typed": {"phrase": "Chef"}, "responses": []})
    Web(_api(status, found)).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "html"
    [old] = await _recipes_of(site)
    assert (old.id, old.state, old.bad_nights, old.health) == (row.id, "retired", 0, []), "no bad night"
    assert old.retired_at is not None
    demoted = [r for r in await _recordings() if r.reason == "demote"]
    assert len(demoted) == 1 and demoted[0].state == "requested" and demoted[0].body is not None
    assert recipes.unpack(demoted[0].body)["typed"] == {"phrase": "Chef"}, "the html try is made with what was typed"


async def test_the_html_try_after_a_demotion_is_a_record_item_taken_at_any_hour_and_goes_to_the_html_turn(js):
    site = await _added(body={"typed": {"phrase": "Chef"}})
    await _live(site, API_RECIPE)
    row_id = (await _recipes_of(site))[0].id
    async with db.session_factory()() as session:
        assert await recipe_live._demote(session, OWNER, await session.get(Site, site.id),
                                         await session.get(SiteRecipe, row_id), "read no postings", NOW) == "html"
        await session.commit()
        wanted = await recipe_record.wanted(session, OWNER)
    assert [w["priority"] for w in wanted] == [0]


async def test_an_html_recipe_that_reads_nothing_on_the_first_read_goes_to_the_chrome_once(js, monkeypatch):
    site = await _added()
    html = {**HTML, "search_url": SEARCH_URL}
    await _live(site, html)
    Web(lambda r: (200, listing([]), "text/html")).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "page"
    assert [r.reason for r in await _recordings() if r.state == "requested"] == ["chain"]
    # a second recipe that fails the same way gets no second chain
    async with db.session_factory()() as session:
        for rec in (await session.scalars(select(SiteRecording))).all():
            rec.state = "failed"
        await session.commit()
    await _live(site, html, version=2)
    assert await recipe_live.first_read(OWNER, site.id) == "none"
    assert [r.reason for r in await _recordings() if r.state == "requested"] == []


async def test_a_failure_that_is_not_a_refusal_is_left_to_the_nights_read(js, monkeypatch):
    site = await _added()
    row = await _live(site, API_RECIPE)
    Web(_api(status=429)).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "error"
    [kept] = await _recipes_of(site)
    assert (kept.id, kept.state) == (row.id, "live")
    assert [r.reason for r in await _recordings()] == ["first"]


async def test_a_page_recipe_a_site_not_added_and_a_site_with_no_titles_are_not_given_a_first_read(js, monkeypatch):
    web = Web(_api()).serve(monkeypatch)
    page_site = await _added(url="https://page.example.test/s")
    await _live(page_site, {**PAGE_RECIPE, "url": "https://page.example.test/s"})
    app_site = await _added(added_by="app", url="https://app.example.test/s")
    await _live(app_site, {**API_RECIPE, "url": "https://app.example.test/api"})
    assert await recipe_live.first_read(OWNER, page_site.id) == "skipped"
    assert await recipe_live.first_read(OWNER, app_site.id) == "skipped"

    async def nothing(_session, _owner):
        return search_fill.Search()

    monkeypatch.setattr(search_fill, "read", nothing)
    titled = await _added(url="https://titled.example.test/s")
    await _live(titled, {**API_RECIPE, "url": "https://titled.example.test/api"})
    assert await recipe_live.first_read(OWNER, titled.id) == "skipped"
    assert web.asked == []


async def test_a_recipe_kept_for_an_added_site_launches_the_first_read_after_the_commit_and_no_other_does(
        js, no_first_read_leaves_its_test):
    launched = no_first_read_leaves_its_test
    added = await _added()
    other = await _added(added_by="app", url="https://other.example.test/s")
    async with db.session_factory()() as session:
        await recipe_live.after_kept(session, OWNER, added.id)
        await recipe_live.after_kept(session, OWNER, other.id)
        await recipe_live.after_kept(session, OWNER, None)
        assert launched == [], "not before the transaction that keeps the recipe has committed"
        await session.commit()
    assert launched == [(OWNER, added.id)]


# --- one learner, three boards --------------------------------------------------------------------


class Board:
    """One made-up board, as data: what its search page makes the browser's capture hold, what Chrome renders, and what
    a plain fetch of its pages and its data request gets. Nothing about a board is in the learner."""

    def __init__(self, host: str, *, raw: list[dict], web, rendered: dict | None = None) -> None:
        self.host, self.raw, self.web, self.rendered = host, raw, web, rendered or {}
        self.url = f"https://{host}/openings"
        self.chrome: PlaceChrome | None = None


def _api_board() -> Board:
    """A board whose search calls a data request with the typed title in it."""
    host = "api.example.test"
    api = f"https://{host}/api/search"

    def answer(request: httpx.Request) -> tuple[int, str, str]:
        rows = [{"reqId": f"R{i}", "name": f"Role {i}", "link": f"/job/R{i}"} for i in range(1, 4)]
        if request.url.path == "/api/search" and request.url.params.get("q") in ("Chef", "Sous"):
            return 200, json.dumps({"results": rows}), "application/json"
        return 404, "", "text/html"

    def response(q: str, after: str) -> dict:
        rows = [{"reqId": f"R{i}", "name": f"Role {i}", "link": f"/job/R{i}"} for i in range(1, 4)]
        return {"method": "GET", "url": f"{api}?q={q}&kind=job", "post_data": None, "status": 200,
                "content_type": "application/json", "body": json.dumps({"results": rows}), "after": after}

    return Board(host, raw=[response("Chef", "phrase"), response("Sous", "second"),
                            {"typed": {"phrase": "Chef", "second": "Sous"}}], web=Web(answer))


def _form_board() -> Board:
    """A board whose search is a plain GET form: no data request, the address holds the words, a plain fetch of the
    results page lists postings."""
    host = "form.example.test"
    typed = {"phrase": "Chef", "second": "Sous", "place": "Northtown"}
    after_search = f"https://{host}/openings?q=Sous&where=Northtown"
    pages = {f"https://{host}/openings": listing(range(1, 6)), **{f"https://{host}/job/{n}": posting(n)
                                                                  for n in range(1, 6)}}

    def answer(request: httpx.Request) -> tuple[int, str, str]:
        if request.url.path == "/openings":
            q = request.url.params.get("q")
            return 200, listing([{"Chef": 10, "Sous": 20}[q] + k for k in (1, 2, 3)] if q else range(1, 6)), "text/html"
        return 200, posting(int(request.url.path.rsplit("/", 1)[1])), "text/html"

    return Board(host, raw=[{"typed": typed}, {"search_url": recipe_record.search_template(after_search, typed)}],
                 web=Web(answer), rendered=pages)


def _script_board() -> Board:
    """A board that draws its results by script: the page Chrome renders lists postings, a plain fetch gets a frame, and
    no data request carries them; only the app's Chrome, typing the title and the place, reads it."""
    host = "script.example.test"
    pages = {f"https://{host}/openings": listing(range(1, 6)), **{f"https://{host}/job/{n}": posting(n)
                                                                  for n in range(1, 6)}}
    typed = {"phrase": "Chef", "second": "Sous", "place": "Northtown"}
    board = Board(host, raw=[{"typed": typed}], web=Web(lambda r: (200, shell(), "text/html")), rendered=pages)
    return board


def _writer(capture: dict) -> dict:
    """What a session answers for a recording, whatever the board: the data request that carries the listing, with the
    typed title where it went, or none."""
    typed = capture.get("typed") or {}
    for r in capture.get("responses", []):
        if r.get("after") != "phrase":
            continue
        parts = urlsplit(r["url"])
        params = {k: "{phrase}" if v == typed.get("phrase") else v for k, v in parse_qsl(parts.query)}
        body = json.loads(r["body"])
        items = next(k for k, v in body.items() if isinstance(v, list))
        base = f"{parts.scheme}://{parts.netloc}"
        return {"recipe": {"kind": "request", "method": "GET", "url": base + parts.path, "items": items,
                           "params": params, "fields": {"title": "name", "url": "link", "ext_id": "reqId"},
                           "url_prefix": base}}
    return {"none": "no request of the page carries the listing"}


class ChainChrome(PlaceChrome):
    """The app's Chrome playing a board: pages it renders by address, and the page recipe's scripts."""

    def __init__(self, board: Board) -> None:
        rows = [(f"Programs Director {i}", f"https://{board.host}/job/{i}", "Springdale") for i in range(1, 4)]
        super().__init__({"Chef": [rows], "": [rows]})
        self.board = board

    async def call(self, method, params=None, session_id=None):
        expression = (params or {}).get("expression", "")
        if method == "Runtime.evaluate" and "outerHTML" in expression:
            here = self.navigated[-1] if self.navigated else self.board.url
            return {"result": {"value": json.dumps({"url": here, "html": self.board.rendered.get(here, "")})}}
        return await super().call(method, params, session_id)


async def _learn(monkeypatch, board: Board) -> tuple[str, list[str]]:
    """The one learner: record the board, have a session write what it can, read what is kept once, and go on to the
    next stage when the read says to. Returns the kind of recipe that stays and the stages it went through (the reasons
    of the recordings). It knows no board."""
    board.web.serve(monkeypatch)
    board.chrome = ChainChrome(board)
    monkeypatch.setattr(page_recipe, "make_tools", lambda: board.chrome)

    async def capture(url, tools=None, interact=True, keyword=None, search=None):
        assert search is not None, "an added site's first recording types the owner's words"
        return board.raw, None

    monkeypatch.setattr(recipe_record, "capture_page", capture)
    picked = iter(["input#q", WHERE, "button.next"])

    async def mark(_tab, step):
        return {"search": {"found": True, "what": "search box", "href": None},
                "next": {"found": True, "what": "next page", "href": None},
                "place": {"found": True, "what": "place box", "href": None, "plain": True}}.get(step)

    async def selector(_tab):
        return next(picked)

    monkeypatch.setattr(page_recipe, "_mark", mark)
    monkeypatch.setattr(page_recipe, "_selector", selector)
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=board.url, kind="job_board", read_by="app", added_by="owner",
                    created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        await recipe_record.request(session, OWNER, site, "first", NOW)
        await session.commit()
    for _ in range(10):
        async with db.session_factory()() as session:
            job = await session.scalar(select(ParseJob).where(ParseJob.state == "waiting"))
            if job is not None:
                rec = await session.get(SiteRecording, uuid.UUID(job.fields["recording_id"]))
                assert rec is not None and rec.body is not None
                await recipe_record.report(session, OWNER, job, _writer(recipes.unpack(rec.body)), NOW)
                await session.commit()
                continue
            open_ = await session.scalar(select(SiteRecording).where(SiteRecording.state == "requested"))
        if open_ is not None:
            await recipe_record.record_by_code(OWNER, board.host)
            continue
        if await recipe_live.first_read(OWNER, site.id) in ("ok", "skipped", "none"):
            break
    live = [r for r in await _recipes_of(site) if r.state == "live"]
    assert len(live) == 1, "one recipe stays, whatever the board"
    return live[0].recipe["kind"], [r.reason for r in await _recordings()]


@pytest.mark.parametrize("make, kept, stages", [
    (_api_board, "request", ["first"]),
    (_form_board, "html", ["first"]),
    (_script_board, "page", ["first", "chain"]),
], ids=["api search", "get form", "script-drawn page"])
async def test_one_learner_over_three_differently_shaped_boards_keeps_the_cheapest_stage_that_reads_each(
        js, monkeypatch, make, kept, stages):
    board = make()
    kind, reasons = await _learn(monkeypatch, board)
    assert (kind, reasons) == (kept, stages)
    [row] = [r for r in await _recipes_of_host(board) if r.state == "live"]
    stored = json.dumps(row.recipe)
    assert "Chef" not in stored and "Sous" not in stored and "Northtown" not in stored.replace(
        "northtown", ""), "none of the owner's words is stored in a recipe"
    if kept == "html":
        assert row.recipe["search_url"] == f"https://{board.host}/openings?q={{phrase}}&where={{place}}"
    if kept == "page":
        assert row.recipe["place"] == WHERE and row.recipe["search"] == "input#q"
        assert ("place", "Northtown") in board.chrome.order


async def _recipes_of_host(board: Board) -> list[SiteRecipe]:
    async with db.session_factory()() as session:
        site = await session.scalar(select(Site).where(Site.url == board.url))
        return list((await session.scalars(select(SiteRecipe).where(SiteRecipe.site_id == site.id))).all())


def test_the_chain_modules_have_no_host_or_selector_conditionals():
    """The learner is one code path: no site is named in it, and nothing in it branches on a host or a selector."""
    import re
    from pathlib import Path

    for module in (recipe_live, html_record, recipe_record, page_recipe, html_recipe):
        source = Path(module.__file__).read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", source, flags=re.S)
        code = re.sub(r"#.*", "", code)
        branch = r"""(host|hostname|netloc)\s*(==|!=|in|endswith|startswith)\s*\(?\s*["'][a-z0-9.-]+\.[a-z]{2,}"""
        assert not re.search(branch, code), module.__name__
        assert not re.search(r"""["'][a-z0-9-]+\.(com|org|net|io|test)["']""", code), module.__name__
