"""The date window `{since}` in a board read by its HTML (docs/PRD.md 37.17): the search address and the fragment
address a recording leaves may hold it, code finds it by comparing the address before the recency filter was changed
with the one after, and a read fills it from when the board was last read well. Every site is made up
(`*.example.test`);
nothing here reaches the network, a model or a browser."""

from datetime import UTC, datetime, timedelta

import pytest

from app.jobsearch.browser.recipes import html_record
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.sites import recipes, search_fill, skills
from tests.jobsearch.test_fragment_search import HTML, TYPED, _asked

NOW = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
CHOICES = [{"value": "", "seconds": None}, {"value": "r86400", "seconds": 86400},
           {"value": "r604800", "seconds": 604800}, {"value": "r2592000", "seconds": 2_592_000}]
SINCE = {"format": "choices", "before": "", "after": "r604800", "choices": CHOICES}
TYPED_SINCE = {**TYPED, "since": SINCE}
HOST = "https://frag.example.test"
SEARCH_URL = f"{HOST}/results?q={{phrase}}&where={{place}}&tpr={{since}}"


# --- the address bar --------------------------------------------------------------------------------------------------


def test_the_entry_the_filters_change_moved_in_the_address_becomes_since():
    before = f"{HOST}/results?q=Sous&where=Northtown"
    after = f"{HOST}/results?q=Sous&where=Northtown&tpr=r604800"
    assert recipe_record.search_template(after, TYPED_SINCE, before) == SEARCH_URL


def test_an_entry_that_already_held_the_changed_value_or_another_value_before_is_not_the_filter():
    same = f"{HOST}/results?q=Sous&where=Northtown&tpr=r604800"
    assert recipe_record.search_template(same, TYPED_SINCE, same) == (
        f"{HOST}/results?q={{phrase}}&where={{place}}&tpr=r604800")
    other = f"{HOST}/results?q=Sous&where=Northtown&tpr=r86400"
    assert "{since}" not in (recipe_record.search_template(same, TYPED_SINCE, other) or "")


def test_with_no_address_from_before_the_change_nothing_in_the_address_is_taken_for_the_filter():
    after = f"{HOST}/results?q=Sous&where=Northtown&tpr=r604800"
    assert "{since}" not in (recipe_record.search_template(after, TYPED_SINCE) or "")
    assert "{since}" not in (recipe_record.search_template(after, TYPED) or "")


def test_the_fragment_address_the_page_asked_after_the_change_keeps_the_filter_and_the_paging_as_blanks():
    before = f"{HOST}/ajax/jobs?q=Sous&where=Northtown&start=0"
    after = f"{HOST}/ajax/jobs?q=Sous&where=Northtown&tpr=r604800&start=0"
    assert recipe_record.fragment_template(after, TYPED_SINCE, before) == (
        f"{HOST}/ajax/jobs?q={{phrase}}&where={{place}}&tpr={{since}}&start={{offset}}")


def test_the_piece_asked_for_after_the_change_has_the_one_before_it_at_the_same_endpoint_as_its_earlier_address():
    pieces = [{"url": f"{HOST}/ajax/jobs?q=Chef", "after": "phrase"},
              {"url": f"{HOST}/ajax/jobs?q=Sous", "after": "second"},
              {"url": f"{HOST}/ajax/jobs?q=Sous&tpr=r604800", "after": "since"}]
    assert html_record.earlier_url(pieces, pieces[2]) == pieces[1]["url"]
    assert html_record.earlier_url(pieces, pieces[1]) is None


# --- the recipe -------------------------------------------------------------------------------------------------------


def test_the_search_address_carries_its_entry_made_by_code_and_a_recording_without_a_filter_makes_none():
    got = html_record.with_template(HTML, "search_url", SEARCH_URL, TYPED_SINCE)
    assert got is not None and got["since"] == {"format": "choices", "choices": CHOICES}
    assert isinstance(skills.parse_recipe(got), skills.HtmlRecipe)
    assert html_record.with_template(HTML, "search_url", SEARCH_URL, TYPED) is None
    plain = html_record.with_template(HTML, "search_url", f"{HOST}/results?q={{phrase}}", TYPED)
    assert plain is not None and "since" not in plain


FRAGMENT = f"{HOST}/ajax/jobs?q={{phrase}}&where={{place}}&tpr={{since}}&start={{offset}}"


def _recorded() -> list[dict]:
    asked = _asked("frag.example.test", "Sous", "since", where="Northtown")
    asked["url"] = asked["url"].replace("&start=0", "&tpr=r604800&start=0")
    return recipes.sanitize([asked], "x")["fragments"]


def test_a_fragment_address_with_since_is_proved_by_the_piece_the_page_asked_after_the_change():
    recipe = {**HTML, "fragment_url": FRAGMENT, "since": {"format": "choices", "choices": CHOICES}}
    problems, rows = html_record.fragment_problems(recipe, TYPED_SINCE, _recorded())
    assert problems == [] and rows == 3
    moved = {**recipe, "fragment_url": FRAGMENT.replace("tpr={since}", "tpr=r604800").replace("start", "ask")}
    problems, _rows = html_record.fragment_problems({k: v for k, v in moved.items() if k != "since"}, TYPED_SINCE,
                                                    _recorded())
    assert problems and "every blank must sit where the typed words were" in problems[0]


def test_a_since_blank_with_no_filter_found_in_the_recording_is_refused():
    problems, _rows = html_record.fragment_problems(
        {**HTML, "fragment_url": FRAGMENT, "since": {"format": "choices", "choices": CHOICES}}, TYPED, _recorded())
    assert problems and "no recency filter" in problems[0]


async def test_the_recorded_piece_replayed_through_the_address_filled_with_the_recorded_value_reads_its_cards():
    recipe = {**HTML, "fragment_url": FRAGMENT, "since": {"format": "choices", "choices": CHOICES}}
    assert await html_record.replay_fragment(recipe, TYPED_SINCE, _recorded()) == 3


# --- a read -----------------------------------------------------------------------------------------------------------


def _read_recipe() -> skills.HtmlRecipe:
    parsed = skills.parse_recipe({**HTML, "search_url": SEARCH_URL, "since": {"format": "choices", "choices": CHOICES}})
    assert isinstance(parsed, skills.HtmlRecipe)
    return parsed


def test_a_read_starts_each_search_at_the_address_filled_with_the_choice_that_covers_the_time_since_the_last_read():
    recipe = _read_recipe()
    three_days = search_fill.Search(("chef",), ("Northtown",), since=NOW - timedelta(days=3), now=NOW)
    since = html_recipe.since_of(recipe, three_days)
    assert since == "r604800"
    assert html_recipe.start_of(recipe, "chef", "Northtown", since) == (
        f"{HOST}/results?q=chef&where=Northtown&tpr=r604800")


def test_a_first_read_takes_the_widest_choice_which_here_is_no_filter_at_all_and_leaves_its_entry_out():
    recipe = _read_recipe()
    first = search_fill.Search(("chef",), ("Northtown",))
    since = html_recipe.since_of(recipe, first)
    assert since == ""
    assert html_recipe.start_of(recipe, "chef", "Northtown", since) == f"{HOST}/results?q=chef&where=Northtown"


def test_a_recipe_with_no_date_window_asks_for_none():
    plain = skills.parse_recipe({**HTML, "search_url": f"{HOST}/results?q={{phrase}}"})
    assert isinstance(plain, skills.HtmlRecipe)
    assert html_recipe.since_of(plain, search_fill.Search(("chef",), since=NOW)) is None


def test_the_next_page_of_a_fragment_search_keeps_the_window():
    recipe = skills.parse_recipe({**HTML, "fragment_url": FRAGMENT, "since": {"format": "choices",
                                                                              "choices": CHOICES}})
    assert isinstance(recipe, skills.HtmlRecipe)
    got = html_recipe.fragment_next(recipe, ("chef", "Northtown"), 1, 10, set(), "r604800")
    assert got == f"{HOST}/ajax/jobs?q=chef&where=Northtown&tpr=r604800&start=10"


def test_an_address_may_not_hold_a_since_blank_in_its_path():
    with pytest.raises(ValueError, match="query only"):
        skills.parse_recipe({**HTML, "since": {"format": "date"}, "search_url": f"{HOST}/{{since}}/r?q={{phrase}}"})
