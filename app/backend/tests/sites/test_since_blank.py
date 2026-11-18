"""The date window blank `{since}` (docs/PRD.md 37.17): what a recency filter's labels say, how code finds the field the
filter moved in a recording's requests, what a recipe may hold, and how a run fills it from when the board was last
read well (`sites/search_fill.py`, `sites/skills.py`). Every site is made up (`*.example.test`) behind a mock transport;
no network, no browser."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.jobsearch.fetch.polite import Fetcher
from app.sites import search_fill, skills
from app.sites.search_fill import Search
from app.sites.skills import parse_recipe, run_recipe

HOST = "https://jobs.example.test"
API = f"{HOST}/api/search"
NOW = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)

DAY, WEEK, MONTH = 86400, 604800, 2_592_000
LIST = {"format": "choices", "choices": [
    {"value": "", "seconds": None}, {"value": "r86400", "seconds": DAY}, {"value": "r604800", "seconds": WEEK},
    {"value": "r2592000", "seconds": MONTH}]}
NO_ANY = {"format": "choices", "choices": [{"value": "1", "seconds": DAY}, {"value": "7", "seconds": WEEK}]}


def _spec(raw: dict) -> skills.Since:
    return skills.Since(**raw)


def _ago(**delta) -> Search:
    return Search(("chef",), since=NOW - timedelta(**delta), now=NOW)


# --- what the labels say ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label, seconds", [
    ("Past 24 hours", DAY), ("Last 7 days", WEEK), ("Past week", WEEK), ("Past month", MONTH), ("3 days ago", 3 * DAY),
    ("Today", DAY), ("Within 2 weeks", 2 * WEEK), ("an hour", 3600), ("Any time", None), ("All", None)])
def test_a_label_says_how_far_back_a_choice_reaches(label, seconds):
    assert search_fill.parse_window(label) == (True, seconds)


@pytest.mark.parametrize("label", ["Within 25 miles", "Remote", "Newest first", "", None])
def test_a_label_that_says_nothing_of_time_is_not_a_window(label):
    assert search_fill.parse_window(label) == (False, None)


def test_only_the_options_whose_labels_say_a_window_are_choices_and_each_value_is_kept_once():
    options = [{"value": "", "label": "Any time"}, {"value": "r86400", "label": "Past 24 hours"},
               {"value": "x", "label": "Sort by"}, {"value": "r86400", "label": "Past day"},
               {"value": "r604800", "label": "Past week"}]
    assert search_fill.choices_of(options) == [{"value": "", "seconds": None}, {"value": "r86400", "seconds": DAY},
                                               {"value": "r604800", "seconds": WEEK}]


# --- what a recording learns --------------------------------------------------------------------------------------


def test_a_learned_filter_keeps_its_format_and_for_a_list_its_choices_with_the_changed_value_among_them():
    got = search_fill.clean_since({"format": "choices", "before": "", "after": "r604800", "choices": LIST["choices"]})
    assert got == {"format": "choices", "before": "", "after": "r604800", "choices": LIST["choices"]}
    assert search_fill.clean_since({"format": "date", "before": "", "after": "2026-11-10"}) == {
        "format": "date", "before": "", "after": "2026-11-10"}


@pytest.mark.parametrize("raw", [
    None, {"format": "weeks", "before": "", "after": "1"}, {"format": "days", "before": "", "after": " "},
    {"format": "choices", "before": "", "after": "r1", "choices": [{"value": "r1", "seconds": DAY}]},  # one choice
    {"format": "choices", "before": "", "after": "zzz", "choices": LIST["choices"]},  # changed to none of them
    {"format": "choices", "before": "", "after": "", "choices": LIST["choices"]},
    {"format": "choices", "before": "", "after": "r1", "choices": [{"value": "r1", "seconds": None},
                                                                    {"value": "r2", "seconds": DAY}]}])
def test_a_filter_with_no_usable_change_is_not_learned(raw):
    assert search_fill.clean_since(raw) is None


def test_the_typed_words_keep_the_filter_only_when_it_is_usable():
    typed = {"phrase": "Chef", "since": {"format": "days", "before": "", "after": "7"}}
    assert search_fill.clean_typed(typed)["since"] == {"format": "days", "before": "", "after": "7"}
    assert "since" not in search_fill.clean_typed({"phrase": "Chef", "since": {"format": "x"}})


def test_the_recipes_entry_is_made_from_what_was_learned_and_a_board_with_no_filter_has_none():
    typed = search_fill.clean_typed({"phrase": "Chef", "since": {"format": "choices", "before": "", "after": "r604800",
                                                                 "choices": LIST["choices"]}})
    assert search_fill.since_spec(typed) == LIST
    assert search_fill.since_spec(search_fill.clean_typed({"phrase": "Chef"})) is None
    assert search_fill.since_spec(None) is None


# --- finding the field the filter moved -----------------------------------------------------------------------------

TYPED = {"phrase": "Chef", "second": "Sous", "place": "North",
         "since": {"format": "choices", "before": "", "after": "r604800", "choices": LIST["choices"]}}


def _leaves(**q) -> dict:
    return {("q", k): v for k, v in q.items()}


def test_the_blank_is_the_field_that_moved_with_the_filter_and_nothing_else():
    last = _leaves(q="Sous", where="North", page="1", tpr="")
    changed = _leaves(q="Sous", where="North", page="1", tpr="r604800")
    where = search_fill.blank_places(_leaves(q="Chef"), _leaves(q="Sous"), _leaves(q="Sous", where="North"), TYPED,
                                     last=last, with_since=changed)
    assert where["since"] == {("q", "tpr")}


def test_a_field_the_request_did_not_have_before_is_the_filter_when_it_holds_the_changed_value():
    where = search_fill.blank_places(None, None, None, TYPED, last=_leaves(q="Sous"),
                                     with_since=_leaves(q="Sous", tpr="r604800"))
    assert where["since"] == {("q", "tpr")}


def test_a_field_that_held_the_changed_value_already_or_held_another_value_before_is_not_the_filter():
    already = search_fill.blank_places(None, None, None, TYPED, last=_leaves(tpr="r604800"),
                                       with_since=_leaves(tpr="r604800"))
    other = search_fill.blank_places(None, None, None, TYPED, last=_leaves(tpr="r86400"),
                                     with_since=_leaves(tpr="r604800"))
    assert already["since"] == set() and other["since"] == set()


def test_a_recording_that_changed_no_filter_has_no_since_blank_to_find():
    plain = {"phrase": "Chef", "second": None, "place": None}
    assert "since" not in search_fill.blank_places(_leaves(q="Chef"), None, None, plain, last=_leaves(q="Chef"),
                                                   with_since=_leaves(q="Chef"))


# --- what a recipe may hold -----------------------------------------------------------------------------------------


def _recipe(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": API, "items": "jobs", "since": LIST,
            "params": {"q": "{phrase}", "tpr": "{since}"}, "url_prefix": HOST,
            "fields": {"title": "title", "url": "path", "ext_id": "id"}}
    return {**base, **over}


def test_a_recipe_with_a_since_blank_needs_its_entry_and_one_with_the_entry_needs_the_blank():
    r = parse_recipe(_recipe())
    assert isinstance(r, skills.RequestRecipe) and r.blanks() == {"phrase", "since"} and r.page is None
    with pytest.raises(ValueError, match="go together"):
        parse_recipe(_recipe(since=None))
    with pytest.raises(ValueError, match="go together"):
        parse_recipe(_recipe(params={"q": "{phrase}"}))


@pytest.mark.parametrize("over, why", [
    ({"url": f"{HOST}/api/{{since}}/search", "params": {"q": "{phrase}"}}, "path"),
    ({"url": "https://{since}.example.test/api"}, "host"),
    ({"since": {"format": "choices", "choices": []}}, "reaches a window"),
    ({"since": {"format": "date", "choices": LIST["choices"]}}, "only a list"),
    ({"since": {"format": "date", "number": True}}, "seconds or days")])
def test_a_since_blank_is_refused_where_it_cannot_be_filled_and_an_entry_that_cannot_be_read(over, why):
    with pytest.raises(ValueError, match=why):
        parse_recipe(_recipe(**over))


def test_an_html_search_address_holds_a_since_blank_only_with_its_entry():
    url = f"{HOST}/search?q={{phrase}}&tpr={{since}}"
    base = {"kind": "html", "url": f"{HOST}/careers", "links": "a.job"}
    ok = parse_recipe({**base, "search_url": url, "since": LIST})
    assert isinstance(ok, skills.HtmlRecipe) and ok.since is not None
    with pytest.raises(ValueError, match="go together"):
        parse_recipe({**base, "search_url": url})
    with pytest.raises(ValueError, match="go together"):
        parse_recipe({**base, "search_url": f"{HOST}/search?q={{phrase}}", "since": LIST})


# --- the value a read fills it with ------------------------------------------------------------------------------


def test_a_choice_list_is_filled_with_the_smallest_choice_that_covers_the_window():
    spec = _spec(LIST)
    assert search_fill.since_value(spec, _ago()) == "r86400"  # read just now: the day, less the margin it adds
    assert search_fill.since_value(spec, _ago(hours=30)) == "r604800"
    assert search_fill.since_value(spec, _ago(days=3)) == "r604800"  # a night missed is not lost
    assert search_fill.since_value(spec, _ago(days=10)) == "r2592000"


def test_a_window_no_choice_covers_takes_the_widest_and_any_time_is_the_widest_of_all():
    assert search_fill.since_value(_spec(LIST), _ago(days=90)) == ""  # "any time": its value is empty, so no entry
    assert search_fill.since_value(_spec(NO_ANY), _ago(days=90)) == "7"


def test_a_first_read_takes_the_widest_choice():
    assert search_fill.since_value(_spec(LIST), Search(("chef",))) == ""
    assert search_fill.since_value(_spec(NO_ANY), Search(("chef",))) == "7"


def test_seconds_days_and_a_date_are_filled_from_the_window_and_have_no_value_for_a_first_read():
    window = 3 * DAY + DAY  # three days since the read, and the margin
    assert search_fill.since_value(_spec({"format": "seconds"}), _ago(days=3)) == str(window)
    assert search_fill.since_value(_spec({"format": "days"}), _ago(days=3)) == "4"
    assert search_fill.since_value(_spec({"format": "days"}), _ago(hours=1)) == "2"  # rounded up, never short
    assert search_fill.since_value(_spec({"format": "date"}), _ago(days=3)) == "2026-11-13"
    for fmt in ("seconds", "days", "date"):
        assert search_fill.since_value(_spec({"format": fmt}), Search(("chef",))) is None


def test_a_recordings_replay_is_filled_with_exactly_the_recorded_value():
    assert search_fill.since_value(_spec(LIST), Search(("chef",), since_literal="r604800")) == "r604800"


def test_the_last_good_read_is_the_newest_night_that_was_neither_bad_nor_blocked():
    health = [{"at": "2026-11-10T03:00:00+00:00", "rows": 5}, {"at": "2026-11-15T03:00:00+00:00", "rows": 0,
              "why": "it read no postings"}, {"at": "2026-11-14T03:00:00+00:00", "rows": 0, "why": "empty"},
              {"at": "2026-11-16T03:00:00+00:00", "why": "blocked"}]
    assert search_fill.last_good_read(health) == datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
    assert search_fill.last_good_read([]) is None and search_fill.last_good_read(None) is None
    assert search_fill.last_good_read([{"at": "garbage"}, {"rows": 3}]) is None


# --- a run fills it ---------------------------------------------------------------------------------------------


def _fetcher(seen: list[httpx.Request]) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request)
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Role 1", "path": "/job/1"}]})

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


async def test_a_night_reads_with_the_choice_that_covers_the_time_since_the_last_good_read():
    seen: list[httpx.Request] = []
    await run_recipe(_fetcher(seen), _recipe(), search=_ago(days=3))
    assert seen[0].url.params["tpr"] == "r604800" and seen[0].url.params["q"] == "chef"


async def test_a_first_read_asks_for_the_widest_choice_and_an_empty_one_leaves_the_entry_out():
    seen: list[httpx.Request] = []
    await run_recipe(_fetcher(seen), _recipe(), search=Search(("chef",)))
    assert "tpr" not in seen[0].url.params  # "any time": the filter is left as the site has it
    seen.clear()
    await run_recipe(_fetcher(seen), _recipe(since=NO_ANY), search=Search(("chef",)))
    assert seen[0].url.params["tpr"] == "7"


async def test_a_number_of_days_is_a_number_in_a_json_body_when_the_page_sent_one():
    recipe = _recipe(method="POST", params=None, body={"q": "{phrase}", "filters": {"age": "{since}"}},
                     since={"format": "days", "number": True})
    sent: list[dict] = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Role 1", "path": "/job/1"}]})

    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)
    await run_recipe(fetch, recipe, search=_ago(days=3))
    assert sent[0]["filters"]["age"] == 4
    sent.clear()
    await run_recipe(fetch, recipe, search=Search(("chef",)))
    assert sent[0] == {"q": "chef", "filters": {}}  # no first-read value: the entry's value is left out


async def test_a_date_is_written_as_the_site_wants_it_in_the_address():
    seen: list[httpx.Request] = []
    await run_recipe(_fetcher(seen), _recipe(since={"format": "date"}), search=_ago(days=3))
    assert seen[0].url.params["tpr"] == "2026-11-13"


def test_an_html_search_address_is_filled_and_leaves_the_entry_out_when_there_is_no_value():
    template = f"{HOST}/search?q={{phrase}}&tpr={{since}}&sort=new"
    assert skills.search_address(template, "sous chef", None, since="r604800") == (
        f"{HOST}/search?q=sous%20chef&tpr=r604800&sort=new")
    assert skills.search_address(template, "sous chef", None) == f"{HOST}/search?q=sous%20chef&sort=new"
    assert skills.search_address(template, "chef", None, since="") == f"{HOST}/search?q=chef&sort=new"
