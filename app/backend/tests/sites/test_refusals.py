"""A learned board's refusal table (`sites/refusals.py`, docs/PRD.md 37.17): the closed table of statuses and page
words, that a learned entry can only make the app more careful, how a rest, a skip and a stop are told apart, that
three rests in a row end the run, and that an empty page is never a refusal nor a refusal an empty page. Every host is
made up (`example.test`); no network."""

from types import SimpleNamespace

import pytest

from app.jobsearch.fetch import host_stops
from app.sites import added, recipes, refusals, skills
from app.sites.refusals import EMPTY, OK, OTHER, REST, SKIP, STOP, Guard, SkipSearch

HOST = "board.example.test"
SIGNIN_PAGE = "<html><body><h1>Please sign in to continue</h1><form></form></body></html>"
EMPTY_PAGE = "<html><body><ul class='results'></ul><p>No results found for your search.</p></body></html>"
SHELL = "<html><body><div id='app'></div><script>render()</script></body></html>"
CHECK = "<html><body>Just a moment... verify you are human</body></html>"


class Fetch:
    """The polite fetcher as a guard sees it: it can be slowed and told a host refused."""

    def __init__(self) -> None:
        self.slowed: list[tuple[str, float, float]] = []
        self.refused_hosts: set[str] = set()

    def slow_host(self, host: str, factor: float, most: float) -> float:
        self.slowed.append((host, factor, most))
        return most


@pytest.fixture
def stops(monkeypatch):
    """What the guard told the host-stop record: (host, status)."""
    told: list[tuple[str, int]] = []

    async def record(host: str, status: int) -> None:
        told.append((host, status))

    monkeypatch.setattr(host_stops, "record_fetch_refusal", record)
    return told


def guard(*learned, fetch=None) -> Guard:
    return Guard(refusals.table_for(*learned), HOST, skills.SkillError, fetch)


# --- the table is data, closed ---------------------------------------------------------------------------------------


def test_a_table_is_a_closed_object_of_statuses_to_a_class_and_lists_of_words():
    kept = refusals.sanitize({"statuses": {"451": "stop", 418: "skip"}, "signin": ["Members  Only"],
                              "challenge": ["Pardon our interruption"],
                              "empty": ["Nothing here today", "Nothing here today"]})
    assert kept == {"statuses": {"451": "stop", "418": "skip"}, "signin": ["members only"],
                    "challenge": ["pardon our interruption"], "empty": ["nothing here today"]}
    assert refusals.sanitize(None) == {} and refusals.sanitize({}) == {}


@pytest.mark.parametrize("bad", [
    {"hosts": ["x.example.test"]}, {"statuses": {"200": "rest"}}, {"statuses": {"404": "rest"}},
    {"statuses": {"410": "stop"}}, {"statuses": {"700": "rest"}}, {"statuses": {"abc": "rest"}},
    {"statuses": {"451": "ignore"}}, {"statuses": {str(n): "rest" for n in range(450, 460)}},
    {"signin": "sign in"}, {"signin": [3]}, {"empty": ["no"]}, {"empty": ["x" * 81]},
    {"challenge": [f"word number {n}" for n in range(13)]}, "text", ["list"]])
def test_a_table_with_anything_but_its_four_keys_or_a_status_a_page_cannot_have_is_refused(bad):
    with pytest.raises(ValueError):
        refusals.sanitize(bad)


def test_a_recipe_carries_its_table_checked_and_one_that_is_not_closed_is_refused():
    recipe = {"kind": "html", "url": f"https://{HOST}/jobs", "links": "a.job", "refusals": {"signin": ["Members Only"]}}
    parsed = skills.parse_recipe(recipe)
    assert isinstance(parsed, skills.HtmlRecipe) and parsed.refusals == {"signin": ["members only"]}
    with pytest.raises(ValueError):
        skills.parse_recipe({**recipe, "refusals": {"statuses": {"200": "stop"}}})
    assert skills.parse_recipe({**recipe, "refusals": {}}).refusals is None  # type: ignore[union-attr]


def test_a_record_that_is_not_a_table_is_read_as_none_never_as_an_error():
    assert refusals.lenient({"statuses": {"200": "rest"}}) == {} and refusals.lenient("x") == {}
    assert refusals.table_for("garbage", None, {"statuses": {"403": "bad"}}) == refusals.DEFAULT


# --- the defaults, and what a learned entry can do to them ----------------------------------------------------------


def test_the_defaults_rest_on_429_and_503_and_stop_on_401_and_403():
    assert refusals.DEFAULT.statuses == {429: REST, 503: REST, 401: STOP, 403: STOP}


def test_a_learned_status_can_raise_a_default_and_never_lower_it():
    table = refusals.table_for({"statuses": {"403": "rest", "401": "skip", "429": "stop", "503": "skip"}})
    assert table.statuses[403] == STOP and table.statuses[401] == STOP, "a stop stays a stop"
    assert table.statuses[429] == STOP and table.statuses[503] == SKIP, "a rest may be raised"


def test_a_learned_status_the_defaults_do_not_name_is_added_at_the_class_it_asks():
    table = refusals.table_for({"statuses": {"451": "stop", "502": "rest"}}, {"statuses": {"451": "rest"}})
    assert table.statuses[451] == STOP and table.statuses[502] == REST


def test_learned_words_are_added_and_no_default_word_is_ever_taken_out():
    table = refusals.table_for({"signin": ["members only"], "challenge": ["pardon our interruption"],
                                "empty": ["nothing here today"]})
    assert "members only" in table.signin and set(refusals.SIGNIN_WORDS) <= set(table.signin)
    assert "pardon our interruption" in table.challenge and set(refusals.CHALLENGE_WORDS) <= set(table.challenge)
    assert "nothing here today" in table.empty and set(refusals.EMPTY_WORDS) <= set(table.empty)


def test_a_learned_empty_word_that_a_refusal_also_says_or_that_is_one_word_is_dropped():
    table = refusals.table_for(
        {"empty": ["captcha results", "sign in to continue now", "nothing", "no results at all"]})
    assert "captcha results" not in table.empty, "it holds a bot check's word: a refusal is never an empty page"
    assert "sign in to continue now" not in table.empty and "nothing" not in table.empty
    assert "no results at all" in table.empty


def test_two_tables_combine_to_the_more_careful_of_each():
    got = refusals.combine({"statuses": {"451": "rest"}, "empty": ["a b c d"]},
                           {"statuses": {"451": "stop", "502": "rest"}, "empty": ["a b c d", "e f g h"]})
    assert got == {"statuses": {"451": "stop", "502": "rest"}, "empty": ["a b c d", "e f g h"]}
    assert refusals.combine(None, "junk") == {}


# --- what an address and a page say -------------------------------------------------------------------------------


@pytest.mark.parametrize(("final", "asked", "sent"), [
    (f"https://{HOST}/login?next=/jobs", f"https://{HOST}/jobs", True),
    (f"https://{HOST}/account/sign-in", f"https://{HOST}/jobs", True),
    (f"https://{HOST}/jobs", f"https://{HOST}/jobs", False),
    (f"https://{HOST}/login", f"https://{HOST}/login", False),  # it asked for that page
    (f"https://{HOST}/loginfo/jobs", f"https://{HOST}/jobs", False),  # a word of a longer part is no sign-in page
    (None, f"https://{HOST}/jobs", False)])
def test_a_read_sent_on_to_a_sign_in_address_it_did_not_ask_for_was_sent_to_a_sign_in_page(final, asked, sent):
    assert refusals.signin_address(final, asked) is sent


# --- the guard: what to do with an answer --------------------------------------------------------------------------


async def test_a_rest_slows_the_host_and_says_ask_again(stops):
    fetch = Fetch()
    assert await guard(fetch=fetch).status(429) == REST and await guard(fetch=fetch).status(503) == REST
    assert fetch.slowed == [(HOST, 2.0, refusals.REST_CAP_SECONDS)] * 2 and stops == []


async def test_a_status_the_table_does_not_name_is_left_to_the_run(stops):
    assert await guard().status(200) is None and await guard().status(404) is None and await guard().status(500) is None


@pytest.mark.parametrize("status", [401, 403])
async def test_a_stop_ends_the_run_and_stops_the_host_until_the_next_off_hours(stops, status):
    fetch = Fetch()
    with pytest.raises(skills.SkillError) as stop:
        await guard(fetch=fetch).status(status)
    assert stop.value.status == status and stop.value.refused is True  # type: ignore[attr-defined]
    assert stops == [(HOST, status)] and HOST in fetch.refused_hosts


async def test_a_skip_leaves_the_search_and_stops_nothing(stops):
    one = guard({"statuses": {"418": "skip"}})
    with pytest.raises(SkipSearch):
        await one.status(418)
    assert one.skipped and stops == []


async def test_three_rests_in_a_row_end_the_run_and_a_good_page_between_them_starts_the_count_again(stops):
    one = guard(fetch=Fetch())
    assert await one.status(429) == REST and await one.status(503) == REST
    with pytest.raises(skills.SkillError) as end:
        await one.status(429)
    assert end.value.status == 429 and end.value.refused is True and "3 times in a row" in str(end.value)  # type: ignore[attr-defined]
    assert stops == [], "the end of a run of rests rests the site; it does not stop the host"
    again = guard(fetch=Fetch())
    await again.status(429)
    await again.status(429)
    assert await again.page("", None, None, 5) == OK
    assert await again.status(429) == REST and await again.status(429) == REST


async def test_a_learned_table_cannot_make_a_stop_a_rest(stops):
    with pytest.raises(skills.SkillError):
        await guard({"statuses": {"403": "rest"}}, {"statuses": {"401": "skip"}}).status(403)
    with pytest.raises(skills.SkillError):
        await guard({"statuses": {"401": "skip"}}).status(401)


# --- the guard: what a page with no postings is -------------------------------------------------------------------


async def test_a_page_with_postings_is_results_whatever_it_says(stops):
    page = "<html><nav><a href='/login'>Please sign in</a></nav>captcha no results found</html>"
    assert await guard().page(page, f"https://{HOST}/login", f"https://{HOST}/jobs", 3) == OK


async def test_a_bot_check_with_no_postings_stops_the_host_and_is_never_an_empty_page(stops):
    with pytest.raises(skills.SkillError) as stop:
        await guard().page(CHECK + " No results found", None, None, 0)
    assert stop.value.status == 403 and stops == [(HOST, 403)]


async def test_a_sign_in_page_by_its_words_or_its_address_skips_the_search_and_is_never_an_empty_page(stops):
    one = guard()
    with pytest.raises(SkipSearch):
        await one.page(SIGNIN_PAGE + " No results found", None, None, 0)
    with pytest.raises(SkipSearch):
        await one.page(SHELL, f"https://{HOST}/login", f"https://{HOST}/jobs", 0)
    assert len(one.skipped) == 2 and one.empty == 0 and stops == []


async def test_a_page_that_says_there_is_nothing_is_empty_and_a_page_that_says_nothing_is_not_explained(stops):
    one = guard()
    assert await one.page(EMPTY_PAGE, None, None, 0) == EMPTY and (one.empty, one.unexplained) == (1, 0)
    assert await one.page(SHELL, None, None, 0) == OTHER and (one.empty, one.unexplained) == (1, 1)


async def test_the_words_in_a_script_or_a_style_are_not_what_the_page_says(stops):
    page = "<html><script>var s = 'no results found';</script><style>.x{}</style><body></body></html>"
    assert await guard().page(page, None, None, 0) == OTHER


async def test_a_later_page_with_no_postings_is_the_end_of_the_results_not_an_empty_search(stops):
    one = guard()
    assert await one.page(EMPTY_PAGE, None, None, 0, first=False) == EMPTY
    assert (one.empty, one.unexplained) == (0, 0)


async def test_a_learned_word_is_what_a_site_says_and_a_default_word_still_holds(stops):
    one = guard({"empty": ["nothing to show today"], "signin": ["members only area"]})
    assert await one.page("<p>Nothing to show today</p>", None, None, 0) == EMPTY
    assert await one.page(EMPTY_PAGE, None, None, 0) == EMPTY
    with pytest.raises(SkipSearch):
        await one.page("<p>Members only area</p>", None, None, 0)


async def test_a_proper_empty_list_of_data_is_an_empty_search_unless_the_answer_is_a_refusal(stops):
    one = guard()
    assert await one.page("[]", None, None, 0, data_empty=True) == EMPTY and one.empty == 1
    with pytest.raises(SkipSearch):
        await one.page("please sign in to continue", None, None, 0, data_empty=True)


async def test_a_run_that_read_nothing_is_said_empty_only_when_the_site_said_so_for_every_search(stops):
    one = guard()
    await one.page(EMPTY_PAGE, None, None, 0)
    assert refusals.said(one.answer(SimpleNamespace(postings=[]))) is True
    await one.page(SHELL, None, None, 0)
    assert refusals.said(one.answer(SimpleNamespace(postings=[]))) is False, "one page nothing explains: not empty"
    two = guard()
    await two.page(EMPTY_PAGE, None, None, 0)
    assert refusals.said(two.answer(SimpleNamespace(postings=[{"title": "x"}]))) is False
    assert refusals.said(guard().answer(SimpleNamespace(postings=[]))) is False, "a run that saw nothing says nothing"


async def test_when_every_search_was_sent_to_a_sign_in_page_the_run_says_why(stops):
    one = guard()
    for _ in range(2):
        with pytest.raises(SkipSearch):
            await one.page(SIGNIN_PAGE, None, None, 0)
    assert "sign-in page" in (one.skipped_all(2) or "") and one.skipped_all(3) is None


def test_a_rest_waits_longer_each_time_to_a_cap_when_a_read_has_no_pace_of_its_own():
    assert [refusals.wait_for(n, 5.0) for n in (1, 2, 3, 9)] == [10.0, 20.0, 40.0, refusals.REST_CAP_SECONDS]


# --- how a night judges it, and what the overlay keeps ---------------------------------------------------------------


def test_a_night_the_site_said_empty_is_not_a_bad_night_and_one_that_read_nothing_still_is():
    assert recipes.judge_night([], 0, None, None, said_empty=True) is None
    assert recipes.judge_night([{"rows": 30}] * 3, 0, None, None, said_empty=True) is None
    assert recipes.judge_night([], 0, None, None) == "it read no postings"
    assert recipes.judge_night([], 0, None, "boom", said_empty=True) == "boom", "an error is never an empty page"
    assert recipes.judge_night([{"rows": 30}] * 3, 4, None, None, said_empty=True) is not None, "rows are judged"


def test_a_run_a_table_ended_is_a_block_whatever_its_status():
    ended = skills.SkillError("rested three times", 503)
    assert recipes.blocked(ended) is False
    ended.refused = True  # type: ignore[attr-defined]
    assert recipes.blocked(ended) is True


def _row(**over) -> SimpleNamespace:
    row = {"url": f"https://{HOST}/jobs", "added_by": "owner", "list_name": "search", "on": True, "polite": None,
           "id": 1}
    return SimpleNamespace(**{**row, **over})


class _Registry:
    def for_host(self, _host):
        return None

    def refusal(self, _host):
        return None


def test_the_overlay_keeps_the_more_careful_table_of_the_sites_record_and_its_recipe():
    row = _row(polite={"refusals": {"statuses": {"451": "rest"}, "signin": ["members only"]}})
    learned = {1: {"refusals": {"statuses": {"451": "stop"}, "empty": ["nothing to show today"]}}}
    kept = added.entries_of([row], _Registry(), learned)[HOST].refusals
    assert kept == {"statuses": {"451": "stop"}, "signin": ["members only"], "empty": ["nothing to show today"]}


def test_a_record_that_is_not_a_table_adds_nothing_to_the_overlay():
    row = _row(polite={"refusals": {"statuses": {"200": "stop"}}})
    assert added.entries_of([row], _Registry())[HOST].refusals == {}


def test_a_host_gets_its_own_table_and_a_neighbour_on_its_domain_gets_the_tables_of_the_domain():
    try:
        added.replace({HOST: added.Added(refusals={"signin": ["members only"]}),
                       "other.example.test": added.Added(refusals={"signin": ["staff only"]})})
        assert added.refusals_of(HOST) == {"signin": ["members only"]}
        assert added.refusals_of("api.example.test") == {"signin": ["members only", "staff only"]}
        assert added.refusals_of("api.elsewhere.test") == {}
    finally:
        added.clear()


def test_the_page_words_a_bot_check_has_are_the_same_the_recorder_looks_for():
    from app.jobsearch.browser.recipes import record

    for word in refusals.CHALLENGE_WORDS:
        assert record.looks_walled(f"<html><body>{word}</body></html>"), word
    assert not record.looks_walled("<html><body>Programs Director</body></html>")
