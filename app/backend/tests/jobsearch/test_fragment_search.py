"""A board whose page asks a script endpoint for a small piece of HTML holding the cards (a fragment), and the employer
and id a card names. Every site is made up (`*.example.test`); nothing here reaches the network, a model or a real
browser: the fetcher is `httpx.MockTransport` and Chrome is `FakeChrome`."""

import json
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser.recipes import html_record
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.postings import index as postings
from app.sites import recipes, search_fill, skills
from app.sites.models import Site
from tests.jobsearch.test_board_chain import (  # noqa: F401  (`quick` is a fixture, used by name)
    Board,
    Web,
    _learn,
    _recipes_of_host,
    quick,
)
from tests.jobsearch.test_html_recipes import posting, shell
from tests.jobsearch.test_page_recipes import RECIPE as PAGE_RECIPE
from tests.jobsearch.test_page_recipes import FakeChrome

TYPED = {"phrase": "Chef", "second": "Sous", "place": "Northtown"}


def cards(numbers, *, company: bool = True, same_id: dict | None = None) -> str:
    """A small piece of HTML holding one card for each number: its link, place, employer and the board's own id."""
    rows = "".join(
        f'<li class="row" data-job-id="J{(same_id or {}).get(n, n)}"><a class="job" href="/job/{n}">'
        f'Programs Director {n}</a><span class="job-location">Springdale</span>'
        + (f'<span class="job-company">Acme {n % 2} Co</span>' if company else "") + "</li>" for n in numbers)
    return f'<ul class="results">{rows}</ul>'


def page_of(numbers, **kw) -> str:
    return f"<html><body><nav><a href='/about-us'>About the company</a></nav>{cards(numbers, **kw)}</body></html>"


def _asked(host: str, q: str, after: str, *, where: str | None = "Northtown", start: int = 0, kind="where") -> dict:
    query = f"q={q}" + (f"&{kind}={where}" if where else "") + f"&start={start}"
    return {"method": "GET", "url": f"https://{host}/ajax/jobs?{query}", "post_data": None, "status": 200,
            "content_type": "text/html; charset=utf-8", "body": cards([1, 2, 3]), "after": after}


def _fragment_board(host: str = "frag.example.test", *, place_id: bool = False) -> Board:
    """A board whose start page draws its cards by script: Chrome renders them, a plain fetch gets a frame, and the page
    asked `/ajax/jobs` for the pieces of HTML that hold them."""
    typed = TYPED
    raw: list = [_asked(host, "Chef", "phrase", where=None), _asked(host, "Sous", "second", where=None),
                 _asked(host, "Sous", "place", where="L4405" if place_id else "Northtown", kind="loc" if place_id
                        else "where"), {"typed": typed}]
    raw[3:3] = []
    pages = {f"https://{host}/openings": page_of(range(1, 6)),
             **{f"https://{host}/job/{n}": posting(n) for n in range(1, 6)}}

    def answer(request: httpx.Request) -> tuple[int, str, str]:
        if request.url.path == "/ajax/jobs":
            base = {"Chef": 10, "Sous": 20}.get(request.url.params.get("q") or "", 0)
            start = int(request.url.params.get("start") or 0)
            if start == 0:
                return 200, cards([base + 1, base + 2, base + 3]), "text/html"
            return 200, "<ul></ul>", "text/html"
        if request.url.path.startswith("/job/"):
            return 200, posting(int(request.url.path.rsplit("/", 1)[1])), "text/html"
        return 200, shell(), "text/html"

    return Board(host, raw=raw, web=Web(answer), rendered=pages)


# --- the recording keeps a fragment ----------------------------------------------------------------


def test_a_script_calls_answer_of_small_html_holding_job_cards_is_kept_as_a_fragment_beside_the_responses():
    raw = [_asked("frag.example.test", "Chef", "phrase", where=None), {"typed": TYPED}]
    got = recipes.sanitize(raw, "https://frag.example.test/openings")
    assert got["responses"] == [] and len(got["fragments"]) == 1
    [piece] = got["fragments"]
    assert piece["after"] == "phrase" and "Programs Director 1" in piece["body"] and piece["url"].endswith("start=0")


def test_what_is_not_a_small_list_of_cards_is_not_kept_and_a_capture_holds_a_few_pieces_at_most():
    host = "frag.example.test"
    not_cards = {**_asked(host, "Chef", "phrase"), "body": "<p>Nothing to see here</p>"}
    post = {**_asked(host, "Chef", "phrase"), "method": "POST", "post_data": "q=Chef"}
    token = {**_asked(host, "Chef", "phrase"), "url": f"https://{host}/ajax/jobs?q=Chef&token=abcdef0123456789abcd"}
    big = {**_asked(host, "Chef", "phrase"), "body": cards(range(1, 6)) + "<!--" + "x" * recipes.FRAGMENT_MAX + "-->"}
    many = [_asked(host, f"T{i}", "phrase", where=None) for i in range(recipes.FRAGMENTS_MAX + 2)]
    got = recipes.sanitize([not_cards, post, token, big], "https://frag.example.test/openings")
    assert "fragments" not in got and not got["responses"]
    got = recipes.sanitize(many, "https://frag.example.test/openings")
    assert len(got["fragments"]) == recipes.FRAGMENTS_MAX and got["dropped"] == {"over the limit": 2}


def test_the_recorder_reads_a_script_calls_html_answer_when_it_holds_cards_and_nothing_else_html():
    xhr = {"kind": "XHR", "finished": True, "status": 200, "content_type": "text/html", "method": "GET",
           "url": "https://frag.example.test/ajax/jobs?q=Chef"}
    assert recipe_record.reads_body(xhr) and not recipe_record.reads_body({**xhr, "kind": "Document"})
    assert recipe_record.keeps(xhr, cards([1, 2, 3])) == cards([1, 2, 3])
    assert recipe_record.keeps(xhr, "<p>menu</p>") is None


def test_the_address_the_piece_was_asked_at_becomes_a_template_with_the_words_and_the_first_page_as_blanks():
    address = "https://frag.example.test/ajax/jobs?q=Sous&where=Northtown&start=0"
    assert recipe_record.fragment_template(address, TYPED) == (
        "https://frag.example.test/ajax/jobs?q={phrase}&where={place}&start={offset}")
    paged = "https://frag.example.test/ajax/jobs?q=Chef&page=1"
    assert recipe_record.fragment_template(paged, TYPED) == "https://frag.example.test/ajax/jobs?q={phrase}&page={page}"
    assert recipe_record.fragment_template("https://frag.example.test/ajax/jobs?k=none", TYPED) is None
    assert recipe_record.fragment_template("https://frag.example.test/ajax/Chef?q=Chef", TYPED) is None, "a word left"


# --- the recipe -------------------------------------------------------------------------------------

FRAGMENT = "https://frag.example.test/ajax/jobs?q={phrase}&where={place}&start={offset}"
HTML = {"kind": "html", "url": "https://frag.example.test/openings", "links": "a.job", "row": "li.row",
        "location": "span.job-location", "company": "span.job-company", "job_id": "li.row"}


def test_an_html_recipe_may_hold_a_fragment_address_and_company_and_id_selectors_inside_a_row():
    got = skills.parse_recipe({**HTML, "fragment_url": FRAGMENT})
    assert isinstance(got, skills.HtmlRecipe) and got.fragment_url == FRAGMENT and got.template() == FRAGMENT
    assert got.company == "span.job-company" and got.job_id == "li.row"


@pytest.mark.parametrize("change, why", [
    ({"fragment_url": "https://frag.example.test/ajax/jobs?where={place}"}, "holds {phrase}"),
    ({"fragment_url": "https://frag.example.test/ajax/jobs?q={phrase}&x={cursor}"}, "may hold only"),
    ({"fragment_url": "https://frag.example.test/ajax/{phrase}"}, "query only"),
    ({"fragment_url": "https://frag.example.test/ajax?q={phrase}&p={page}&s={offset}"}, "not both"),
    ({"fragment_url": "https://other.example.test/ajax?q={phrase}"}, "own host"),
    ({"fragment_url": FRAGMENT, "search_url": "https://frag.example.test/o?q={phrase}"}, "not both"),
    ({"fragment_url": FRAGMENT, "row": None, "location": None, "company": None, "job_id": "a.x"}, "inside a row"),
])
def test_a_fragment_address_that_is_not_a_title_search_in_its_query_on_the_site_is_refused(change, why):
    with pytest.raises(ValueError, match=why):
        skills.parse_recipe({**HTML, **change})


def test_a_page_recipe_may_name_a_company_and_an_id_inside_a_row_and_not_outside_one():
    got = skills.parse_recipe({**PAGE_RECIPE, "row": "li.row", "company": "span.who", "job_id": "li.row"})
    assert isinstance(got, skills.PageRecipe) and got.company == "span.who"
    with pytest.raises(ValueError, match="inside a row"):
        skills.parse_recipe({k: v for k, v in {**PAGE_RECIPE, "company": "span.who"}.items() if k != "row"})


# --- the proof, offline and live --------------------------------------------------------------------


def _recorded(**kw) -> list[dict]:
    return recipes.sanitize([_asked("frag.example.test", "Sous", "place", **kw)], "x")["fragments"]


def test_every_blank_must_sit_where_the_typed_words_were_and_the_recorded_cards_must_read():
    problems, rows = html_record.fragment_problems({**HTML, "fragment_url": FRAGMENT}, TYPED, _recorded())
    assert problems == [] and rows == 3
    moved = "https://frag.example.test/ajax/jobs?where={phrase}&q={place}&start={offset}"
    problems, _rows = html_record.fragment_problems({**HTML, "fragment_url": moved}, TYPED, _recorded())
    assert problems and "every blank must sit where the typed words were" in problems[0]
    problems, _rows = html_record.fragment_problems({**HTML, "fragment_url": FRAGMENT, "links": "a.nothing",
                                                     "row": None, "location": None, "company": None,
                                                     "job_id": None}, TYPED, _recorded())
    assert problems and "read 0 postings" in problems[0]


def test_a_fragment_address_with_a_typed_word_left_in_it_is_refused_and_never_kept():
    literal = "https://frag.example.test/ajax/jobs?q={phrase}&kw=Chef&where={place}&start={offset}"
    problems, _rows = html_record.fragment_problems({**HTML, "fragment_url": literal}, TYPED, _recorded())
    assert problems and "'Chef'" in problems[0]
    assert "never stores the owner's titles" in (html_record.refuse_literals({**HTML, "fragment_url": literal}, TYPED)
                                                 or "")
    assert html_record.refuse_literals({**HTML, "fragment_url": FRAGMENT}, TYPED) is None
    problems, _rows = html_record.fragment_problems({**HTML, "fragment_url": FRAGMENT}, {**TYPED, "place": None},
                                                    _recorded())
    assert problems and "{place} is used, but nothing was typed" in problems[0]


async def test_a_recorded_piece_replayed_through_the_filled_address_reads_its_cards_with_no_network():
    got = await html_record.replay_fragment({**HTML, "fragment_url": FRAGMENT}, TYPED, _recorded())
    assert got == 3


# --- learning it ------------------------------------------------------------------------------------


async def test_a_board_whose_page_asks_for_pieces_of_html_is_learned_as_an_html_recipe_that_reads_night_one(
        js, monkeypatch):
    board = _fragment_board()
    kind, reasons = await _learn(monkeypatch, board)
    assert (kind, reasons) == ("html", ["first"]), "the request, then html (a fragment), and no need of the page"
    [row] = [r for r in await _recipes_of_host(board) if r.state == "live"]
    recipe = row.recipe
    assert recipe["fragment_url"] == f"https://{board.host}/ajax/jobs?q={{phrase}}&where={{place}}&start={{offset}}"
    assert "search_url" not in recipe or not recipe["search_url"]
    assert recipe["company"] == "span.job-company" and recipe["job_id"] == "li.row"
    stored = json.dumps(recipe)
    assert "Chef" not in stored and "Sous" not in stored and "Northtown" not in stored
    asked = [a for a in board.web.asked if "/ajax/jobs" in a]
    assert any("q=Chef&where=Northtown&start=0" in a for a in asked), "a plain fetch of the search read the cards"
    assert "q=Sous&where=Northtown&start=0" in asked[-1], "then the second title, to prove the search follows the title"


async def test_the_recording_of_a_fragment_board_keeps_the_pieces_with_the_typed_words_until_it_is_done(
        js, monkeypatch):
    board = _fragment_board()
    await _learn(monkeypatch, board)
    async with db.session_factory()() as session:
        from app.sites.models import SiteRecording

        for rec in (await session.scalars(select(SiteRecording))).all():
            body = recipes.unpack(rec.body) if rec.body else {}
            assert "fragments" not in body and "typed" not in body, "what was typed is gone once it is kept"


async def test_a_board_that_wants_a_place_id_is_read_by_title_alone_and_its_status_says_so_in_one_line(
        js, monkeypatch):
    board = _fragment_board("ident.example.test", place_id=True)
    kind, _reasons = await _learn(monkeypatch, board)
    assert kind == "html"
    [row] = [r for r in await _recipes_of_host(board) if r.state == "live"]
    assert "{place}" not in row.recipe["fragment_url"] and "{phrase}" in row.recipe["fragment_url"]
    async with db.session_factory()() as session:
        site = await session.scalar(select(Site).where(Site.url == board.url))
        assert site is not None and site.read_note == recipe_record.PLACE_UNSUPPORTED
        assert "\n" not in site.read_note


async def test_a_board_that_does_carry_the_place_says_nothing_of_it(js, monkeypatch):
    board = _fragment_board("carry.example.test")
    await _learn(monkeypatch, board)
    async with db.session_factory()() as session:
        site = await session.scalar(select(Site).where(Site.url == board.url))
        assert site is not None and not site.read_note


# --- reading it ---------------------------------------------------------------------------------------


def _pages(request: httpx.Request) -> tuple[int, str, str]:
    start = int(request.url.params.get("start") or 0)
    if start == 0:
        return 200, cards([1, 2, 3]), "text/html"
    if start == 3:  # a card of the first page again, under another address, and one new
        return 200, cards([13, 4], same_id={13: 3}), "text/html"
    return 200, "<ul></ul>", "text/html"


async def test_a_fragment_is_read_page_after_page_by_offset_until_an_answer_has_no_card():
    web = Web(_pages)
    fetch = web.fetcher()
    recipe = skills.parse_recipe({**HTML, "fragment_url": FRAGMENT})
    got = await skills.run_recipe(fetch, recipe, search=search_fill.Search(("Chef de Partie",), ("Northtown",)))
    await fetch.close()
    word = quote("Chef de Partie", safe="")
    assert web.asked == [f"https://frag.example.test/ajax/jobs?q={word}&where=Northtown&start={n}" for n in (0, 3, 5)]
    assert got.truncated, "what a search shows is a partial view of the board"


async def test_two_cards_of_one_id_on_one_page_or_two_are_one_posting_told_apart_by_the_id():
    web = Web(_pages)
    fetch = web.fetcher()
    recipe = skills.parse_recipe({**HTML, "fragment_url": FRAGMENT})
    got = await skills.run_recipe(fetch, recipe, search=search_fill.Search(("Chef",), ("Northtown",)))
    await fetch.close()
    assert sorted(p["ext_id"] for p in got.postings) == ["J1", "J2", "J3", "J4"]


async def test_a_card_that_names_the_employer_files_the_role_under_it_and_none_falls_back_to_the_board():
    web = Web(lambda r: (200, cards([1, 2, 3], company=r.url.params.get("q") != "Plain"), "text/html"))
    named = skills.parse_recipe({**HTML, "fragment_url": FRAGMENT})
    fetch = web.fetcher()
    got = await skills.run_recipe(fetch, named, search=search_fill.Search(("Chef",), ("Northtown",)))
    plain = await skills.run_recipe(fetch, named, search=search_fill.Search(("Plain",), ("Northtown",)))
    await fetch.close()
    assert [p["company"] for p in got.postings] == ["Acme 1 Co", "Acme 0 Co", "Acme 1 Co"]
    assert all("company" not in p for p in plain.postings)
    filed = postings._listed(got, company="Board Name", company_from_postings=True)
    assert [x.name for x in filed] == ["Acme 1 Co", "Acme 0 Co", "Acme 1 Co"]
    unfiled = postings._listed(plain, company="Board Name", company_from_postings=True)
    assert {x.name for x in unfiled} == {"Board Name"}


async def test_an_html_recipe_with_no_company_or_id_selector_reads_and_files_as_it_always_did():
    web = Web(lambda r: (200, cards([1, 2, 3]), "text/html"))
    fetch = web.fetcher()
    bare = {k: v for k, v in HTML.items() if k not in ("company", "job_id")}
    got = await skills.run_recipe(fetch, skills.parse_recipe({**bare, "fragment_url": FRAGMENT}),
                                  search=search_fill.Search(("Chef",), ("Northtown",)))
    await fetch.close()
    assert sorted(p["ext_id"] for p in got.postings) == ["/job/1", "/job/2", "/job/3"]
    assert all("company" not in p for p in got.postings)


def test_the_fragments_next_page_is_one_the_address_pages_by_and_at_most_a_few_pages_deep():
    recipe = skills.parse_recipe({**HTML, "fragment_url": FRAGMENT})
    assert isinstance(recipe, skills.HtmlRecipe)
    next_url = html_recipe.fragment_next(recipe, ("Chef", "Northtown"), 1, 3, set())
    assert next_url == "https://frag.example.test/ajax/jobs?q=Chef&where=Northtown&start=3"
    assert html_recipe.fragment_next(recipe, ("Chef", "Northtown"), html_recipe.FRAGMENT_PAGES, 9, set()) is None
    by_page = skills.parse_recipe({**HTML, "fragment_url": "https://frag.example.test/a?q={phrase}&p={page}"})
    assert isinstance(by_page, skills.HtmlRecipe)
    assert html_recipe.fragment_next(by_page, ("Chef", None), 1, 3, set()) == "https://frag.example.test/a?q=Chef&p=2"
    one_page = skills.parse_recipe({**HTML, "search_url": "https://frag.example.test/o?q={phrase}"})
    assert isinstance(one_page, skills.HtmlRecipe)
    assert html_recipe.fragment_next(one_page, ("Chef", None), 1, 3, set()) is None


def test_a_cards_company_and_id_are_read_from_the_card_and_a_card_without_them_says_none():
    root = html_record.hs.parse(cards([1, 2], company=False))
    parsed = skills.parse_recipe(HTML)
    assert isinstance(parsed, skills.HtmlRecipe)
    rows = html_recipe.rows_of(root, "https://frag.example.test/ajax/jobs", parsed)
    assert [(r["company"], r["job_id"]) for r in rows] == [(None, "J1"), (None, "J2")]


def test_the_recorder_finds_a_company_and_the_row_itself_as_the_id_on_cards_that_carry_them():
    root = html_record.hs.parse(page_of(range(1, 6)))
    found = html_record.row_fields(root, "a.job", "li.row")
    assert found == {"location": "span.job-location", "company": "span.job-company", "job_id": "li.row"}


# --- a page recipe --------------------------------------------------------------------------------


async def test_a_page_recipe_files_a_role_under_the_company_its_row_names_and_dedupes_by_the_id(monkeypatch):
    class Rows(FakeChrome):
        async def call(self, method, params=None, session_id=None):
            expression = (params or {}).get("expression", "")
            if expression.startswith("/*rb:rows*/"):
                args = json.loads(expression.split("\n", 1)[0].split("//", 1)[1])
                assert args[3:] == ["span.who", "li.row"], "the script is told where the company and id are"
                rows = [{"href": "https://careers.example.test/job/1", "title": "Programs Director 1",
                         "location": None, "company": "Acme Co", "job_id": "J1"},
                        {"href": "https://careers.example.test/job/1?ref=x", "title": "Programs Director 1",
                         "location": None, "company": "Acme Co", "job_id": "J1"},
                        {"href": "https://careers.example.test/job/2", "title": "Programs Director 2",
                         "location": None, "company": None, "job_id": None}]
                return {"result": {"value": json.dumps(rows)}}
            return await super().call(method, params, session_id)

    monkeypatch.setattr(page_recipe, "make_tools", lambda: Rows({"": [[]]}))
    recipe = skills.parse_recipe({**PAGE_RECIPE, "row": "li.row", "company": "span.who", "job_id": "li.row"})
    assert isinstance(recipe, skills.PageRecipe)
    got = await page_recipe.run(recipe, [])
    assert [(p["ext_id"], p.get("company")) for p in got.postings] == [
        ("J1", "Acme Co"), (page_recipe.ext_id_of("https://careers.example.test/job/2"), None)]
