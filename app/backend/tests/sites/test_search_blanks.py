"""A request recipe's search blanks (`{phrase}`, `{place}`): what the recipe may hold, how a run fills them from the
owner's search, and how code finds where they go by comparing two typed searches in a recording
(`sites/search_fill.py`, `sites/skills.py`, `sites/recipes.py`). Every site is made up (`*.example.test`) behind a mock
transport or a recording; no network, no browser."""

import json

import httpx
import pytest

from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.profile import schema as profile_schema
from app.sites import recipes, search_fill, skills
from app.sites.search_fill import Search
from app.sites.skills import SkillError, parse_recipe, run_recipe

HOST = "https://jobs.example.test"
PAGE = f"{HOST}/careers"
API = f"{HOST}/api/search"


def _fetcher(handle, seen: list[httpx.Request]) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request)
        return handle(request)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


def _rows(*ids: int) -> list[dict]:
    return [{"id": i, "title": f"Role {i}", "path": f"/job/{i}"} for i in ids]


def _recipe(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": API, "items": "jobs", "params": {"q": "{phrase}"},
            "fields": {"title": "title", "url": "path", "ext_id": "id"}, "url_prefix": HOST}
    return {**base, **over}


# --- what a recipe may hold ---------------------------------------------------------------------------------


def test_a_search_blank_is_allowed_with_no_paging_and_a_recipe_says_which_it_holds():
    r = parse_recipe(_recipe(params={"q": "{phrase}", "l": "{place}"}))
    assert isinstance(r, skills.RequestRecipe) and r.page is None and r.blanks() == {"phrase", "place"}
    assert parse_recipe(_recipe(params=None)).blanks() == set()


@pytest.mark.parametrize("over, why", [
    ({"url": "https://{phrase}.example.test/api"}, "host"),
    ({"url": f"{HOST}/api/{{phrase}}/search"}, "path"),
    ({"page": {"param": "cursor", "size": 10, "cursor_path": "next"}, "params": {"q": "{phrase}", "c": "{cursor}"}},
     "cursor"),
    ({"kind": "embedded", "blob": "script:data"}, "embedded"),
    ({"params": {"q": "{remote}"}}, "not a placeholder"),
])
def test_a_blank_is_refused_in_the_host_the_path_a_cursor_flow_an_embedded_block_and_any_other_name(over, why):
    with pytest.raises(ValueError, match=why):
        parse_recipe(_recipe(**over))


def test_a_blank_is_refused_in_the_detail_address():
    with pytest.raises(ValueError, match="no other placeholder"):
        parse_recipe(_recipe(detail={"url": f"{HOST}/api/job/{{ext_id}}?q={{phrase}}", "description": "body"}))


def test_the_paging_rule_is_as_it_was_for_page_offset_cursor_and_size():
    with pytest.raises(ValueError, match="placeholders go with paging"):
        parse_recipe(_recipe(params={"q": "{phrase}", "start": "{offset}"}))
    with pytest.raises(ValueError, match="the paging is by offset"):
        parse_recipe(_recipe(params={"q": "{phrase}", "p": "{page}"}, page={"param": "offset", "size": 5}))
    ok = parse_recipe(_recipe(params={"q": "{phrase}", "start": "{offset}"}, page={"param": "offset", "size": 5}))
    assert ok.blanks() == {"phrase"}


# --- filling them -------------------------------------------------------------------------------------------


async def test_the_phrase_is_quoted_in_the_address_and_raw_in_params_and_body():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _rows(1)}), seen)
    word = "C# dev & ops"
    await run_recipe(fetch, _recipe(url=f"{API}?kw={{phrase}}", params=None), search=Search((word,)))
    assert "kw=C%23%20dev%20%26%20ops" in str(seen[0].url), "quoted in the address"
    seen.clear()
    await run_recipe(fetch, _recipe(url=f"{API}?kw={{phrase}}", params={"q": "{phrase}", "fixed": "x"}),
                     search=Search((word,)))
    assert seen[0].url.params["kw"] == word and seen[0].url.params["q"] == word, "and read back whole"

    posted: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _rows(1)}), posted)
    await run_recipe(fetch, _recipe(method="POST", params=None, body={"query": "{phrase}", "n": 5}),
                     search=Search((word,)))
    assert json.loads(posted[0].content) == {"query": word, "n": 5}, "raw in the body, the other entries as they were"


async def test_an_empty_place_drops_its_entry_in_params_body_and_address():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _rows(1)}), seen)
    recipe = _recipe(url=f"{API}?x=1&near={{place}}", params={"q": "{phrase}", "l": "{place}"})
    await run_recipe(fetch, recipe, search=Search(("a",)))
    assert dict(seen[0].url.params) == {"x": "1", "q": "a"}

    seen.clear()
    await run_recipe(fetch, recipe, search=Search(("a",), ("Town A",)))
    assert dict(seen[0].url.params) == {"x": "1", "near": "Town A", "q": "a", "l": "Town A"}

    seen.clear()
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _rows(1)}), seen)
    await run_recipe(fetch, _recipe(method="POST", params=None, body={"q": "{phrase}", "where": "{place}"}),
                     search=Search(("a",)))
    assert json.loads(seen[0].content) == {"q": "a"}


async def test_each_phrase_and_place_is_one_search_deduplicated_by_id_and_the_listing_closes_nothing():
    seen: list[httpx.Request] = []

    def handle(request):
        q, place = request.url.params["q"], request.url.params.get("l")
        ids = {("a", None): [1, 2], ("a", "X"): [2, 3], ("b", None): [4], ("b", "X"): [4, 5]}[(q, place)]
        return httpx.Response(200, json={"jobs": _rows(*ids)})

    recipe = _recipe(params={"q": "{phrase}", "l": "{place}"})
    listing = await run_recipe(_fetcher(handle, seen), recipe, search=Search(("a", "b"), ("X",)))
    assert [(r.url.params["q"], r.url.params["l"]) for r in seen] == [("a", "X"), ("b", "X")]
    assert sorted(p["ext_id"] for p in listing.postings) == ["2", "3", "4", "5"]

    seen.clear()
    listing = await run_recipe(_fetcher(handle, seen), _recipe(params={"q": "{phrase}"}), search=Search(("a", "b")))
    assert sorted(p["ext_id"] for p in listing.postings) == ["1", "2", "4"] and listing.truncated, (
        "a search's view of the site is partial: it closes nothing")


async def test_the_loop_is_bounded_by_the_requests_limit_the_time_and_enough():
    seen: list[httpx.Request] = []

    def handle(request):
        n = len(seen)
        return httpx.Response(200, json={"jobs": _rows(n * 10 + 1, n * 10 + 2)})

    many = Search(tuple("abcdef"))
    listing = await run_recipe(_fetcher(handle, seen), _recipe(limits={"requests": 2}), search=many)
    assert len(seen) == 2 and listing.truncated and skills.LIMIT_REACHED in (listing.note or "")

    seen.clear()
    fetch = _fetcher(handle, seen)
    fetch.deadline = 0.0001  # already past
    listing = await run_recipe(fetch, _recipe(), search=many)
    assert len(seen) == 1 and skills.TIME_RAN_OUT in (listing.note or "")

    seen.clear()
    listing = await run_recipe(_fetcher(handle, seen), _recipe(), search=many, enough=3)
    assert len(seen) == 2 and len(listing.postings) == 4 and skills.ENOUGH_READ in (listing.note or "")


async def test_a_recipe_with_a_phrase_blank_cannot_run_without_titles_and_one_with_no_places_runs_once():
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _rows(1)}), [])
    with pytest.raises(SkillError, match="holds none"):
        await run_recipe(fetch, _recipe(), search=Search())
    with pytest.raises(SkillError, match="holds none"):
        await run_recipe(fetch, _recipe())
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _rows(1)}), seen)
    await run_recipe(fetch, _recipe(params={"q": "{phrase}", "l": "{place}"}), search=Search(("a", "b")))
    assert len(seen) == 2 and all("l" not in r.url.params for r in seen)


async def test_a_blank_recipe_pages_each_search_with_the_paging_number_where_it_goes():
    seen: list[httpx.Request] = []

    def handle(request):
        start = int(request.url.params["start"])
        q = request.url.params["q"]
        base = 100 if q == "b" else 0
        return httpx.Response(200, json={"jobs": _rows(*range(base + start, base + min(start + 2, 3)))})

    recipe = _recipe(params={"q": "{phrase}", "start": "{offset}"}, page={"param": "offset", "size": 2})
    listing = await run_recipe(_fetcher(handle, seen), recipe, search=Search(("a", "b")))
    got = [(r.url.params["q"], r.url.params["start"]) for r in seen]
    assert got == [("a", "0"), ("a", "2"), ("b", "0"), ("b", "2")]
    assert len(listing.postings) == 6


async def test_a_recipe_with_no_blank_is_read_as_before_whatever_the_search():
    seen: list[httpx.Request] = []
    fetch = _fetcher(lambda r: httpx.Response(200, json={"jobs": _rows(1, 2)}), seen)
    listing = await run_recipe(fetch, _recipe(params=None), search=Search(("a", "b"), ("X",)))
    assert len(seen) == 1 and len(listing.postings) == 2 and not listing.truncated


# --- the search, read once ----------------------------------------------------------------------------------


def test_the_search_is_read_from_the_profile_by_one_function_the_browser_shares():
    from app.jobsearch.browser import reader as br

    profile = profile_schema.Profile(criteria=profile_schema.Criteria(target_roles=[
        profile_schema.TargetRole(id="a", titles=["First", "Second"])]))
    found = search_fill.of_profile(profile)
    assert found.phrases == ("First", "Second") and found.places == tuple(br._places(profile))
    assert search_fill.of_profile(profile_schema.Profile()).phrases == ()


# --- what a recording says was typed ------------------------------------------------------------------------


def _raw(url: str, rows: list[int], after: str | None = None, **over) -> dict:
    base = {"method": "GET", "url": url, "post_data": None, "status": 200, "content_type": "application/json",
            "body": json.dumps({"jobs": _rows(*rows)}), **({"after": after} if after else {})}
    return {**base, **over}


def _typed(**over) -> dict:
    return {"typed": {"phrase": "Senior Analyst", "second": "Data Clerk", "place": "Town A", **over}}


def _capture(*extra: dict, typed: dict | None = None) -> dict:
    return recipes.sanitize([
        _raw(f"{API}?q=Senior+Analyst&kind=job&lang=en", [1, 2, 3], "phrase"),
        _raw(f"{API}?q=Data+Clerk&kind=job&lang=en", [4, 5], "second"),
        _raw(f"{API}?q=Senior+Analyst&kind=job&lang=en&near=Town%20A", [1, 2], "place"),
        *extra, typed if typed is not None else _typed()], PAGE)


async def _verify(recipe: dict, capture: dict | None = None):
    return await recipes.verify_offline(recipe, capture if capture is not None else _capture())


def _blank(**over) -> dict:
    return _recipe(**{"params": {"q": "{phrase}", "kind": "job", "lang": "en"}, **over})


def test_a_capture_keeps_what_was_typed_and_the_typing_each_request_followed():
    cap = _capture()
    assert cap["typed"] == {"phrase": "Senior Analyst", "second": "Data Clerk", "place": "Town A"}
    assert [r.get("after") for r in cap["responses"]] == ["phrase", "second", "place"]
    assert recipes.unpack(recipes.pack(cap)) == cap
    assert "typed" not in recipes.sanitize([_raw(f"{API}?q=x", [1])], PAGE), "nothing typed, nothing kept"
    assert "typed" not in recipes.sanitize([{"typed": {"phrase": "  "}}], PAGE), "a title that is blank is no title"


def test_what_the_writer_is_given_names_the_typed_words_and_one_line_takes_them_out_again():
    text = recipes.trim(_capture())
    assert '"typed"' in text and "Senior Analyst" in text and '"search_typed": "second"' in text
    stripped = recipes.strip_typed(text)
    assert stripped is not None and '{"typed"' not in stripped and "Data Clerk" not in stripped.split("\n")[1]
    assert recipes.strip_typed(None) is None


async def test_a_blank_where_the_two_searches_differ_reproduces_the_recording():
    verdict = await _verify(_blank())
    assert verdict.ok, verdict.problems
    assert verdict.rows == 3 == verdict.recorded, "the rows of the one search it reproduced, not of every search"


async def test_a_place_blank_is_good_where_the_place_search_added_it():
    verdict = await _verify(_blank(params={"q": "{phrase}", "kind": "job", "lang": "en", "near": "{place}"}))
    assert verdict.ok, verdict.problems
    assert verdict.rows == 2, "filled with the typed title and place, it is the place search that it reproduces"


async def test_a_blank_in_a_field_the_two_searches_did_not_differ_in_is_refused():
    verdict = await _verify(_blank(params={"q": "Senior", "kind": "{phrase}", "lang": "en"}))
    assert not verdict.ok and any("kind" in p and "q" in p for p in verdict.problems), verdict.problems


async def test_a_blank_with_nothing_typed_for_it_or_no_field_to_sit_in_is_refused():
    no_place = _capture(typed=_typed(place=None))
    verdict = await _verify(_blank(params={"q": "{phrase}", "kind": "job", "lang": "en", "near": "{place}"}), no_place)
    assert not verdict.ok and any("{place} is used, but nothing was typed" in p for p in verdict.problems)

    flat = recipes.sanitize([_raw(f"{API}?q=Senior+Analyst&kind=job", [1, 2, 3], "phrase"),
                             _raw(f"{API}?q=Senior+Analyst&kind=job&near=Town%20A&near2=Town%20A", [1], "place"),
                             {"typed": {"phrase": "Senior Analyst", "place": "Town A"}}], PAGE)
    ok = await _verify(_recipe(params={"q": "{phrase}", "kind": "job"}), flat)
    assert ok.ok, ok.problems
    both = await _verify(_recipe(params={"q": "{phrase}", "kind": "job", "near": "{place}", "near2": "{place}"}), flat)
    assert both.ok, f"a place the page's own request carries is a blank, in every field it was in: {both.problems}"
    none_shown = recipes.sanitize([_raw(f"{API}?q=Senior+Analyst&kind=job", [1, 2, 3], "phrase"),
                                   _raw(f"{API}?q=Senior+Analyst&kind=job", [1, 2, 3], "place"),
                                   {"typed": {"phrase": "Senior Analyst", "place": "Town A"}}], PAGE)
    refused = await _verify(_recipe(params={"q": "{phrase}", "kind": "job", "near": "{place}"}), none_shown)
    assert not refused.ok and any("no field of the request that held the typed place" in p for p in refused.problems)


async def test_with_one_title_only_the_field_must_hold_exactly_the_typed_words():
    exact = recipes.sanitize([_raw(f"{API}?q=Senior%20Analyst", [1, 2, 3], "phrase"),
                              {"typed": {"phrase": "Senior Analyst"}}], PAGE)
    assert (await _verify(_recipe(), exact)).ok
    wrapped = recipes.sanitize([_raw(f"{API}?q=title%3Asenior+analyst", [1, 2, 3], "phrase"),
                                {"typed": {"phrase": "Senior Analyst"}}], PAGE)
    refused = await _verify(_recipe(), wrapped)
    assert not refused.ok and any("no field of the request that held the typed phrase" in p for p in refused.problems)


async def test_a_typed_word_that_remains_whole_in_the_request_is_refused_whatever_its_length_or_case_or_encoding():
    for params in ({"q": "{phrase}", "kind": "job", "lang": "en", "t": "senior analyst"},
                   {"q": "{phrase}", "kind": "job", "lang": "en", "t": "SENIOR%20ANALYST"},
                   {"q": "{phrase}", "kind": "job", "lang": "en", "t": "x data+clerk y"}):
        verdict = await _verify(_blank(params=params))
        assert not verdict.ok and any("still in the request" in p for p in verdict.problems), params
    short = recipes.sanitize([_raw(f"{API}?q=Go&kind=job&lang=GO", [1, 2, 3], "phrase"),
                              _raw(f"{API}?q=Go&kind=job&lang=going", [1, 2, 3], "phrase"),
                              {"typed": {"phrase": "Go"}}], PAGE)
    verdict = await _verify(_recipe(params={"q": "{phrase}", "kind": "job", "lang": "GO"}), short)
    assert not verdict.ok and any("'Go'" in p for p in verdict.problems), "two letters are refused too"
    inside = await _verify(_recipe(params={"q": "{phrase}", "kind": "job", "lang": "going"}), short)
    assert inside.ok, "a word inside a longer word is not the typed word whole"
    in_body = recipes.sanitize([
        _raw(f"{API}", [1, 2, 3], "phrase", method="POST", post_data=json.dumps({"search": "Go", "kind": "job"})),
        {"typed": {"phrase": "Go"}}], PAGE)
    held = await _verify(_recipe(method="POST", params=None, body={"search": "{phrase}", "kind": "job", "t": "go"}),
                         in_body)
    assert not held.ok and any("'Go'" in p for p in held.problems)


async def test_a_recipe_with_blanks_is_replayed_exactly_and_one_without_keeps_the_closest_answer():
    extra = _blank(params={"q": "{phrase}", "kind": "job", "lang": "en", "sort": "new"})
    verdict = await _verify(extra)
    assert not verdict.ok and any("never made" in p for p in verdict.problems), verdict.problems

    paged = _recipe(params={"offset": "{offset}", "limit": "{size}", "kind": "job"},
                    page={"param": "offset", "size": 3})
    plain = recipes.sanitize([_raw(f"{API}?offset=0&limit=3&kind=job&sort=new", [1, 2, 3])], PAGE)
    assert (await recipes.verify_offline(paged, plain)).ok, "a paging replay is as tolerant as it was"


async def test_verify_takes_the_typed_words_as_an_optional_argument_that_defaults_to_the_recordings_own():
    cap = _capture()
    assert (await recipes.verify_offline(_blank(), cap)).ok
    assert (await recipes.verify_offline(_blank(), cap, cap["typed"])).ok
    other = {**cap["typed"], "phrase": "Someone Else"}
    assert not (await recipes.verify_offline(_blank(), cap, other)).ok, "the words given are the words judged"
    plain = await recipes.verify_offline(_recipe(params={"kind": "job", "lang": "en", "q": "x"}), recipes.sanitize(
        [_raw(f"{API}?q=x&kind=job&lang=en", [1, 2, 3])], PAGE))
    assert plain.ok, plain.problems
