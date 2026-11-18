"""A board that searches by a place id: the learner picks the site's own matching suggestion (docs/PRD.md 37.16; the
owner, 2026-11-17: "Yes"). The recording types each of the owner's places in the place box and, when the site shows its
own list of suggestions, picks the entry that is the place (exactly, else starting with it) and no other click; code
finds the field of the request that changes with the place and keeps a place-to-value map on the recipe; a run fills
`{place}` from the map and names the places it has no value for; a page recipe picks the entry at run time. The browser
is scripted and every site is made up (`*.example.test`): no network, no Chrome."""

import json

import httpx
import pytest

from app.jobsearch.browser.recipes import interact as recipe_interact
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.sites import learned_line, recipes, search_fill, skills
from app.sites.search_fill import Search
from tests.jobsearch.test_board_chain import WHERE, PlaceChrome, _page, _rows
from tests.jobsearch.test_board_chain import quick as chain_quick  # noqa: F401
from tests.jobsearch.test_page_recipes import RECIPE as PAGE_RECIPE
from tests.jobsearch.test_recipe_interact import API, PAGE, _jobs, quick  # noqa: F401
from tests.jobsearch.test_recipe_typing import PLACE_BOX, TypingPage, _rules
from tests.sites.test_search_blanks import API as DATA_API
from tests.sites.test_search_blanks import _fetcher, _recipe
from tests.sites.test_search_blanks import _rows as _data_rows

#: The board's own value for each entry of its list of suggestions.
IDS = {"Northtown, ST": "NT1", "Remote": "RM9", "Southtown": "ST5"}
OPTIONS = ["Northtown, ST", "Northtownville", "Remote", "Southtown"]
SEARCH = Search(("Chef",), ("Northtown", "Remote"))
LISTED = {**PLACE_BOX, "plain": False}


class PickingPage(TypingPage):
    """A tab whose place box shows the site's own list of suggestions once a place is typed; picking an entry makes the
    data request the page's form would, from the entry's id. Each entry clicked is noted."""

    def __init__(self, finds, options=OPTIONS, **kw) -> None:
        super().__init__(finds, **kw)
        self.options = options
        self.picked: list[str] = []
        self.mouse: list[str] = []

    async def call(self, method, params=None, session_id=None):
        e = (params or {}).get("expression", "")
        if method == "Runtime.evaluate" and "options: []" in e:
            shown = list(self.options) if self.values.get("place") else []
            return {"result": {"value": json.dumps({"options": shown})}}
        if method == "Runtime.evaluate" and "picked:" in e and "data-rb-opt" in e:
            chosen = self.options[int(e.split('data-rb-opt="')[1].split('"')[0])]
            self.picked.append(chosen)
            self.mouse.append(e)
            q = f"?q={self.values.get('search', '')}&loc={IDS.get(chosen, '')}"
            self.request("XHR", f"{API}{q}", 200, "application/json", json.dumps({"results": _jobs(3)}))
            return {"result": {"value": json.dumps({"picked": True})}}
        return await super().call(method, params, session_id)


async def _work(page, **kw):
    return await recipe_interact.work(page, "s", PAGE, new_traffic=lambda: recipe_record.assemble(page.events),
                                      settle=_settle, **kw)


async def _settle(_seconds: float) -> None:
    return None


# --- which entry ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("options, word, want", [
    (["Remote Valley", "Remote", "Remote Hills"], "Remote", 1),  # the exact one, though another starts with it
    (["Northtown, ST", "Northtownville"], "northtown", 0),  # else the first that starts with it, case ignored
    (["  Southtown  "], "Southtown", 0),  # spacing is ignored
    (["Eastport", "West Northtown"], "Northtown", None),  # inside a word is no match
    ([], "Remote", None),
    (["Remote"], "  ", None),
])
def test_the_entry_picked_is_the_exact_one_else_the_first_that_starts_with_the_place_and_never_another(
        options, word, want):
    assert search_fill.pick_suggestion(options, word) == want


def test_the_script_that_clicks_an_entry_clicks_only_an_option_and_the_one_that_reads_the_list_skips_sign_ins():
    pick, listing = recipe_interact._pick_script(3), recipe_interact._SUGGEST_LIST
    assert 'data-rb-opt="3"' in pick and "[role=option]" in pick and "closest('[role=listbox], ul, ol')" in pick
    assert pick.count("dispatchEvent") == 1 and ".click()" not in pick
    assert "NEVER.test(text)" in listing and json.dumps(recipe_interact.NEVER) in listing
    assert "hostname !== location.hostname" in listing, "an entry that links to another site is not offered"


# --- the recording picks ----------------------------------------------------------------------------------------


async def test_each_place_is_typed_and_its_exact_or_starting_entry_picked_and_each_request_is_marked_with_it():
    page = PickingPage(_rules(place=LISTED))
    out = await _work(page, phrases=("Chef",), place="Northtown", more_places=("Remote",))
    assert page.picked == ["Northtown, ST", "Remote"]
    assert [w for box, w in page.words if box == "place"] == ["Northtown", "Remote"]
    assert out.typed == {"phrase": "Chef", "second": None, "place": "Northtown", "more_places": ["Remote"]}
    assert list(out.after.values()) == ["phrase", "place", "place2"]
    assert len(page.mouse) == 2 and "place" not in page.clicks, "no click but the picks: the form was not submitted"


async def test_a_place_with_no_matching_entry_is_not_picked_and_no_click_is_made_outside_the_list():
    page = PickingPage(_rules(place=LISTED), options=["Eastport", "Westville"])
    out = await _work(page, phrases=("Chef",), place="Northtown")
    assert page.picked == [] and page.mouse == [] and "place" not in page.clicks
    assert out.typed is not None and out.typed["place"] is None
    assert "no entry" in out.skipped["place"]


async def test_a_box_that_shows_no_list_is_not_picked_from_and_the_title_search_stands():
    page = PickingPage(_rules(place=LISTED), options=[])
    out = await _work(page, phrases=("Chef",), place="Northtown")
    assert page.mouse == [] and out.typed == {"phrase": "Chef", "second": None, "place": None}
    assert "no list" in out.skipped["place"]


async def test_a_later_place_with_no_entry_is_left_and_the_ones_that_matched_are_kept():
    page = PickingPage(_rules(place=LISTED))
    out = await _work(page, phrases=("Chef",), place="Nowhere", more_places=("Remote", "Southtown"))
    assert page.picked == ["Remote", "Southtown"]
    assert out.typed is not None and (out.typed["place"], out.typed["more_places"]) == ("Remote", ["Southtown"])
    assert list(out.after.values()) == ["phrase", "place", "place2"], "the picks are tagged in the order they were made"


async def test_a_plain_place_box_takes_the_first_place_only_and_picks_nothing():
    page = PickingPage(_rules())
    out = await _work(page, phrases=("Chef",), place="Northtown", more_places=("Remote",))
    assert [w for box, w in page.words if box == "place"] == ["Northtown"] and page.mouse == []
    assert out.typed == {"phrase": "Chef", "second": None, "place": "Northtown"}


async def test_no_more_than_the_most_places_are_picked(monkeypatch):
    monkeypatch.setattr(search_fill, "PLACES_MAX", 2)
    page = PickingPage(_rules(place=LISTED))
    await _work(page, phrases=("Chef",), place="Northtown", more_places=("Remote", "Southtown"))
    assert page.picked == ["Northtown, ST", "Remote"]


async def test_a_page_with_a_password_field_gets_no_place_typed_and_nothing_picked():
    page = PickingPage(_rules(place=LISTED), password=True)
    await _work(page, phrases=("Chef",), place="Northtown")
    assert page.words == [] and page.mouse == []


# --- code finds the field that changes with the place -----------------------------------------------------------


def test_the_one_field_that_differs_between_the_places_is_the_place_and_noise_or_one_place_is_no_proof():
    one = {("q", "q"): "Chef", ("q", "loc"): "NT1", ("q", "page"): "1"}
    two = {("q", "q"): "Chef", ("q", "loc"): "RM9", ("q", "page"): "1"}
    assert search_fill.place_values({"Northtown": one, "Remote": two}) == (("q", "loc"), {"northtown": "NT1",
                                                                                          "remote": "RM9"})
    assert search_fill.place_values({"Northtown": one}) is None, "two places are needed to tell the field from noise"
    noisy = {**two, ("q", "t"): "99"}
    assert search_fill.place_values({"Northtown": {**one, ("q", "t"): "98"}, "Remote": noisy}) is None
    assert search_fill.place_values({"Northtown": one, "Remote": one}) is None, "nothing moved"


def test_a_field_that_is_a_number_in_the_body_is_kept_as_a_number():
    got = search_fill.place_values({"A": {("b", "area.id"): "7"}, "B": {("b", "area.id"): "9"}},
                                   {("b", "area.id"): True})
    assert got == (("b", "area.id"), {"a": 7, "b": 9})


async def _capture(page, search=SEARCH):
    raw, stop = await recipe_record.capture_page(PAGE, page, search=search)
    assert stop is None
    return recipes.sanitize(raw, PAGE)


async def test_the_recording_shows_the_field_and_the_value_each_place_picked_from_the_list_made():
    capture = await _capture(PickingPage(_rules(place=LISTED)))
    assert capture["typed"] == {"phrase": "Chef", "second": None, "place": "Northtown", "more_places": ["Remote"]}
    found = recipes.place_ids(capture, "GET", "/api/search", capture["typed"])
    assert found == (("q", "loc"), {"northtown": "NT1", "remote": "RM9"})


def _writer_recipe(**over) -> dict:
    """What a writer session makes from a recording whose request has no place in it: the title blank only."""
    return {"kind": "request", "method": "GET", "url": API, "items": "results", "params": {"q": "{phrase}"},
            "fields": {"title": "title", "url": "path", "ext_id": "jobId"}, "url_prefix": "https://careers.example.test",
            **over}


async def test_code_puts_the_place_blank_and_the_map_on_the_recipe_and_the_replay_matches_the_recording():
    capture = await _capture(PickingPage(_rules(place=LISTED)))
    recipe = recipes.with_place_map(_writer_recipe(), capture, capture["typed"])
    assert recipe["params"] == {"q": "{phrase}", "loc": "{place}"}
    assert recipe["place_map"] == {"northtown": "NT1", "remote": "RM9"}
    verdict = await recipes.verify_offline(_writer_recipe(), capture)
    assert verdict.ok, verdict.problems


async def test_a_writer_that_left_the_recorded_id_in_the_recipe_has_it_replaced_by_the_blank():
    capture = await _capture(PickingPage(_rules(place=LISTED)))
    recipe = recipes.with_place_map(_writer_recipe(url=f"{API}?loc=NT1"), capture, capture["typed"])
    assert recipe["url"].endswith("?loc={place}") and "NT1" not in json.dumps(recipe["params"])


async def test_a_board_that_shows_the_places_own_words_in_its_request_needs_no_map():
    page = TypingPage(_rules())  # a plain box: the place's words are in the request
    capture = await _capture(page)
    got = recipes.with_place_map(_writer_recipe(params={"q": "{phrase}", "where": "{place}"}), capture,
                                 capture["typed"])
    assert "place_map" not in got


async def test_a_writer_that_put_the_place_blank_in_another_field_is_told_where_it_goes():
    capture = await _capture(PickingPage(_rules(place=LISTED)))
    verdict = await recipes.verify_offline(_writer_recipe(params={"q": "{phrase}", "other": "{place}"}), capture)
    assert not verdict.ok and any("put it there" in p for p in verdict.problems)


async def test_with_one_place_only_nothing_is_mapped_and_the_board_stays_place_unsupported():
    capture = await _capture(PickingPage(_rules(place=LISTED)), Search(("Chef",), ("Northtown",)))
    assert recipes.place_ids(capture, "GET", "/api/search", capture["typed"]) is None
    got = recipes.with_place_map(_writer_recipe(), capture, capture["typed"])
    assert "place_map" not in got and "{place}" not in json.dumps(got)
    assert recipe_record.place_note(capture["typed"], skills.parse_recipe(got).blanks()) == (
        recipe_record.PLACE_UNSUPPORTED)


# --- a run fills the blank from the map ------------------------------------------------------------------------


MAPPED = {"northtown": "NT1", "remote": 9}


def _map_recipe(**over) -> dict:
    return _recipe(params={"q": "{phrase}", "loc": "{place}"}, **{"place_map": MAPPED, **over})


async def test_a_run_fills_the_place_blank_with_the_boards_own_value_for_each_place_it_has_one_for():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _data_rows(1)}), seen)
    await skills.run_recipe(fetch, _map_recipe(), search=Search(("a",), ("Northtown", "REMOTE")))
    assert [(r.url.params["q"], r.url.params["loc"]) for r in seen] == [("a", "NT1"), ("a", "9")]
    assert str(seen[0].url).startswith(DATA_API)


async def test_a_place_with_no_value_is_not_searched_and_the_read_names_it():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _data_rows(1)}), seen)
    got = await skills.run_recipe(fetch, _map_recipe(), search=Search(("a",), ("Northtown", "Eastport", "Westville")))
    assert [r.url.params["loc"] for r in seen] == ["NT1"]
    assert "Eastport, Westville" in (got.note or "") and "Northtown" not in (got.note or "")


async def test_when_no_place_has_a_value_nothing_is_searched_and_the_places_are_named():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _data_rows(1)}), seen)
    got = await skills.run_recipe(fetch, _map_recipe(), search=Search(("a",), ("Eastport",)))
    assert seen == [] and got.postings == [] and "Eastport" in (got.note or "")


async def test_a_search_that_names_no_place_is_read_once_with_the_place_entry_left_out():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _data_rows(1)}), seen)
    await skills.run_recipe(fetch, _map_recipe(), search=Search(("a",)))
    assert len(seen) == 1 and "loc" not in seen[0].url.params


async def test_a_number_in_the_body_is_sent_as_a_number():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _data_rows(1)}), seen)
    recipe = _recipe(method="POST", params=None, body={"q": "{phrase}", "area": "{place}"}, place_map=MAPPED)
    await skills.run_recipe(fetch, recipe, search=Search(("a",), ("Remote",)))
    assert json.loads(seen[0].content) == {"q": "a", "area": 9}


def test_a_map_goes_with_the_blank_and_is_cleaned():
    with pytest.raises(ValueError, match="place_map and"):
        skills.parse_recipe(_recipe(place_map=MAPPED))
    got = skills.parse_recipe(_map_recipe(place_map={" North  Town ": "x", "": "y", "bad": True, "z": " "}))
    assert isinstance(got, skills.RequestRecipe) and got.place_map == {"north town": "x"}


def test_the_learned_line_says_which_places_a_board_is_searched_for():
    line = learned_line.line(_map_recipe(), None)
    assert line is not None and "places searched: northtown, remote" in line and "place unsupported" not in line
    plain = learned_line.line(_recipe(params={"q": "{phrase}"}), None)
    assert plain is not None and "place unsupported" in plain
    page = learned_line.line({**PAGE_RECIPE, "place": WHERE, "place_pick": True}, None)
    assert page is not None and "picked from the site's own list" in page


# --- an html recipe ---------------------------------------------------------------------------------------------


def test_the_address_bar_after_each_pick_makes_the_search_address_with_the_place_blank_and_a_map():
    addresses = {"Northtown": "https://careers.example.test/search?q=Chef&loc=NT1",
                 "Remote": "https://careers.example.test/search?q=Chef&loc=RM9"}
    found = recipe_record._place_ids_in_addresses(addresses)
    assert found == (("q", "loc"), {"northtown": "NT1", "remote": "RM9"})
    typed = {"phrase": "Chef", "second": None, "place": "Northtown", "more_places": ["Remote"]}
    assert recipe_record.search_template(addresses["Remote"], typed, None, "loc") == (
        "https://careers.example.test/search?q={phrase}&loc={place}")
    assert recipe_record.search_template(addresses["Remote"], typed) == (
        "https://careers.example.test/search?q={phrase}&loc=RM9"), "no key, no blank"
    own = {"Northtown": "https://careers.example.test/search?q=Chef&loc=Northtown",
           "Remote": "https://careers.example.test/search?q=Chef&loc=Remote"}
    assert recipe_record._place_ids_in_addresses(own) is None, "the words are in the address: the words are the blank"


def test_the_pieces_a_script_call_answered_for_each_pick_show_the_endpoint_the_field_and_the_values():
    pieces = [{"url": "https://careers.example.test/frag?q=Chef&loc=NT1", "after": "place"},
              {"url": "https://careers.example.test/frag?q=Chef&loc=RM9", "after": "place2"},
              {"url": "https://careers.example.test/other?z=1", "after": "place"}]
    typed = {"phrase": "Chef", "place": "Northtown", "more_places": ["Remote"]}
    assert recipes.html_place_ids(pieces, typed) == ("/frag", ("q", "loc"), {"northtown": "NT1", "remote": "RM9"})


async def test_an_html_recipe_fills_its_search_address_from_the_map_and_names_the_places_it_has_none_for():
    from app.jobsearch.browser.recipes import html_run as html_recipe

    recipe = skills.parse_recipe({"kind": "html", "url": "https://careers.example.test/jobs", "links": "a.job",
                                  "search_url": "https://careers.example.test/s?q={phrase}&loc={place}",
                                  "place_map": {"northtown": "NT1"}})
    assert isinstance(recipe, skills.HtmlRecipe)
    search = Search(("Chef",), ("Northtown", "Eastport"))
    assert html_recipe.searches_of(recipe, search) == [("Chef", "NT1")]
    assert html_recipe.unmapped_of(recipe, search) == ["Eastport"]
    assert html_recipe.start_of(recipe, "Chef", "NT1") == "https://careers.example.test/s?q=Chef&loc=NT1"
    with pytest.raises(ValueError, match="place_map"):
        skills.parse_recipe({"kind": "html", "url": "https://careers.example.test/jobs", "links": "a.job",
                             "search_url": "https://careers.example.test/s?q={phrase}", "place_map": {"a": "b"}})


# --- a page recipe picks at run time ----------------------------------------------------------------------------


class PickChrome(PlaceChrome):
    """`PlaceChrome` whose place box shows the site's own list of suggestions once a place is typed in it."""

    def __init__(self, *a, options=("Northtown, ST", "Southtown"), **kw) -> None:
        super().__init__(*a, **kw)
        self.options = list(options)
        self.picked: list[str] = []
        self.suggest_flags: list = []
        self.typed_place = False

    async def call(self, method, params=None, session_id=None):
        e = (params or {}).get("expression", "")
        if e.startswith("/*rb:type*/"):
            args = json.loads(e.split("\n", 1)[0].split("//", 1)[1])
            if args[0] == WHERE:
                self.typed_place = True
                self.suggest_flags.append(args[3])
        if e.startswith("/*rb:mark*/"):
            return {"result": {"value": json.dumps({"marked": True})}}
        if "options: []" in e:
            return {"result": {"value": json.dumps({"options": self.options if self.typed_place else []})}}
        if "picked:" in e and "data-rb-opt" in e:
            self.picked.append(self.options[int(e.split('data-rb-opt="')[1].split('"')[0])])
            return {"result": {"value": json.dumps({"picked": True})}}
        return await super().call(method, params, session_id)


def test_a_page_recipe_may_pick_from_a_list_only_with_a_place_box():
    assert skills.parse_recipe({**PAGE_RECIPE, "place": WHERE, "place_pick": True}).place_pick is True  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="place_pick goes with a place box"):
        skills.parse_recipe({**PAGE_RECIPE, "place_pick": True})


def test_the_script_that_types_in_a_place_box_lets_a_list_of_suggestions_through_only_when_told_to():
    shut = page_recipe.type_in(WHERE, "x", vetted=True)
    open_ = page_recipe.type_in(WHERE, "x", vetted=True, suggest=True)
    assert json.loads(shut.split("\n", 1)[0].split("//", 1)[1])[3] is False
    assert json.loads(open_.split("\n", 1)[0].split("//", 1)[1])[3] is True
    assert "!A[3] &&" in shut and "aria-autocomplete" in shut, "the one script holds both; the flag decides"


async def test_a_run_types_each_place_and_picks_its_entry_and_a_place_with_no_entry_is_named(monkeypatch):
    fake = _page(monkeypatch, PickChrome({"a": [_rows()]}, options=("Northtown, ST", "Southtown")))
    recipe = skills.parse_recipe({**PAGE_RECIPE, "place": WHERE, "place_pick": True})
    got = await page_recipe.run(recipe, ["a"], places=["Northtown", "Nowhere"])  # type: ignore[arg-type]
    assert fake.picked == ["Northtown, ST"] and fake.suggest_flags == [True, True]
    assert len(got.postings) == 3 and "no entry for: Nowhere" in (got.note or "")


async def test_a_recipe_that_does_not_pick_never_reads_the_list_or_clicks_an_entry(monkeypatch):
    fake = _page(monkeypatch, PickChrome({"a": [_rows()]}))
    await page_recipe.run(skills.parse_recipe({**PAGE_RECIPE, "place": WHERE}), ["a"],  # type: ignore[arg-type]
                          places=["Northtown"])
    assert fake.picked == [] and fake.suggest_flags == [False]


async def test_a_recording_picks_the_place_from_the_list_and_keeps_the_place_box_with_the_pick_flag(monkeypatch):
    found: dict = {"search": {"found": True, "what": "search box", "href": None},
                   "next": {"found": True, "what": "next page", "href": None},
                   "place": {"found": True, "what": "place box", "href": None, "plain": False}}
    chosen = iter(["input#q", WHERE, "button.next"])

    async def mark(_tab, step):
        return found.get(step)

    async def selector(_tab):
        return next(chosen)

    monkeypatch.setattr(page_recipe, "_mark", mark)
    monkeypatch.setattr(page_recipe, "_selector", selector)
    fake = _page(monkeypatch, PickChrome({"Chef": [_rows()], "": [_rows()]}, options=("Northtown, ST",)))
    recipe, why, status = await page_recipe.propose(str(PAGE_RECIPE["url"]), "Chef", "Northtown")
    assert (why, status) == (None, None) and recipe is not None
    assert recipe["place"] == WHERE and recipe["place_pick"] is True and fake.picked == ["Northtown, ST"]


async def test_a_recording_leaves_the_place_box_out_when_its_list_has_no_entry_for_the_place(monkeypatch):
    found: dict = {"search": {"found": True, "what": "search box", "href": None},
                   "next": {"found": True, "what": "next page", "href": None},
                   "place": {"found": True, "what": "place box", "href": None, "plain": False}}
    chosen = iter(["input#q", WHERE, "button.next"])

    async def mark(_tab, step):
        return found.get(step)

    async def selector(_tab):
        return next(chosen)

    monkeypatch.setattr(page_recipe, "_mark", mark)
    monkeypatch.setattr(page_recipe, "_selector", selector)
    fake = _page(monkeypatch, PickChrome({"Chef": [_rows()], "": [_rows()]}, options=("Eastport",)))
    recipe, _why, _status = await page_recipe.propose(str(PAGE_RECIPE["url"]), "Chef", "Northtown")
    assert recipe is not None and "place" not in recipe and "place_pick" not in recipe and fake.picked == []
