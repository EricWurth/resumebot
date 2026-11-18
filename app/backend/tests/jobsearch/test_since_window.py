"""The date window `{since}` through a recording and a night (docs/PRD.md 37.17): the recording changes the site's own
recency filter once, code finds the field of the page's request that moved with it, the recipe is kept with how the
value is written, and each night fills it from when the board was last read well (`jobsearch/browser/recipes/
interact.py`, `record.py`, `sites/recipes.py`, `jobsearch/postings/site_reads.py`). The browser is scripted and every
site is made up (`*.example.test`); no network, no Chrome."""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser.recipes import interact as recipe_interact
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import ParseJob
from app.jobsearch.postings import site_reads
from app.sites import recipes, search_fill
from app.sites.models import Site, SiteRecipe, SiteRecording
from tests.jobsearch.test_recipe_interact import ALL, API, HOST, PAGE, _jobs, quick  # noqa: F401
from tests.jobsearch.test_recipe_typing import PLACE_BOX, TypingPage, _settle
from tests.support import OWNER

DAY, WEEK, MONTH = 86400, 604800, 2_592_000
NOW = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
SEARCH = search_fill.Search(("Chef de Partie", "Sous Chef"), ("Northtown",))
OPTIONS = [{"value": "", "label": "Any time"}, {"value": "r86400", "label": "Past 24 hours"},
           {"value": "r604800", "label": "Past week"}, {"value": "r2592000", "label": "Past month"}]
CHOICES = [{"value": "", "seconds": None}, {"value": "r86400", "seconds": DAY}, {"value": "r604800", "seconds": WEEK},
           {"value": "r2592000", "seconds": MONTH}]
SELECT = {"found": True, "what": "recency filter", "href": None, "kind": "select", "value": "", "hint": "Date posted",
          "options": OPTIONS}


class RecencyPage(TypingPage):
    """A tab with a recency filter: changing it makes the data request the page's form would make, with the filter's
    value in `posted`; or, when `needs_submit`, only the form's search button does."""

    def __init__(self, finds, *, needs_submit: bool = False, **kw) -> None:
        super().__init__(finds, **kw)
        self.needs_submit, self.changed = needs_submit, []

    def _ask(self) -> None:
        v = self.values
        q = f"?q={v.get('search', '')}&where={v.get('place', '')}&posted={v.get('since', '')}"
        self.request("XHR", f"{API}{q}", 200, "application/json", json.dumps({"results": _jobs(3)}))

    async def call(self, method, params=None, session_id=None):
        e = (params or {}).get("expression", "")
        if method == "Runtime.evaluate" and "'recency filter'" in e:
            found = self.finds.get("recency")
            self.last = "recency" if found else self.last
            return {"result": {"value": json.dumps(found or {"found": False})}}
        if method == "Runtime.evaluate" and "set:" in e and "want" in e:  # a list's choice
            self.values["since"] = json.loads(e.split("const want = ", 1)[1].split("; if", 1)[0])
            self.changed.append(self.values["since"])
            if not self.needs_submit:
                self._ask()
            return {"result": {"value": json.dumps({"set": True})}}
        if method == "Runtime.evaluate" and "update|filter|show" in e:  # the form's own search button
            self._ask()
            return {"result": {"value": json.dumps({"clicked": True})}}
        if method == "Runtime.evaluate" and "typed:" in e and self.last == "recency":  # a date or a number typed in
            word = json.loads(e.split("call(el, ", 1)[1].split("); for", 1)[0])
            self.values["since"] = word
            self.changed.append(word)
            if not self.needs_submit:
                self._ask()
            return {"result": {"value": json.dumps({"typed": True})}}
        return await super().call(method, params, session_id)


def _rules(**over):
    return {**ALL, "search": {"found": True, "what": "search box", "href": None}, "place": PLACE_BOX,
            "recency": SELECT, **over}


def _fresh(page):
    """What the page asked since the last look, as the recording's own `new_traffic` gives it."""
    seen: set[str] = set()

    def new_traffic() -> list[dict]:
        fresh = [r for r in recipe_record.assemble(page.events) if r["request_id"] not in seen]
        seen.update(r["request_id"] for r in fresh if r["status"] is not None)
        return fresh

    return new_traffic


async def _work(page, **kw):
    return await recipe_interact.work(page, "s", PAGE, new_traffic=_fresh(page), settle=_settle, **kw)


# --- the recording changes the filter once -------------------------------------------------------------------------


async def test_the_recency_filter_is_changed_once_to_a_value_other_than_its_own_and_what_it_asked_is_marked():
    page = RecencyPage(_rules())
    out = await _work(page, phrases=SEARCH.phrases, place="Northtown")
    assert page.changed == ["r604800"]  # a week is the choice nearest a week; once, never a second change
    assert out.typed["since"] == {"format": "choices", "before": "", "after": "r604800", "choices": CHOICES}
    assert list(out.after.values()) == ["phrase", "second", "place", "since"]
    assert out.before_url == PAGE


async def test_a_page_with_no_recency_filter_has_no_since_and_says_so():
    page = RecencyPage(_rules(recency=None))
    out = await _work(page, phrases=SEARCH.phrases, place="Northtown")
    assert page.changed == [] and "since" not in out.typed and "recency" in out.skipped["since"]
    assert "since" not in list(out.after.values())


async def test_a_list_whose_labels_say_nothing_of_time_is_not_a_recency_filter():
    odd = {**SELECT, "options": [{"value": "a", "label": "Within 10 miles"},
                                 {"value": "b", "label": "Within 50 miles"}]}
    page = RecencyPage(_rules(recency=odd))
    out = await _work(page, phrases=SEARCH.phrases)
    assert page.changed == [] and "since" not in out.typed and "since" in out.skipped


async def test_a_list_with_only_the_value_it_has_offers_nothing_to_change_to():
    only = {**SELECT, "value": "r86400", "options": [{"value": "", "label": "Any time"},
                                                      {"value": "r86400", "label": "Past 24 hours"}]}
    page = RecencyPage(_rules(recency=only))
    out = await _work(page, phrases=SEARCH.phrases)
    assert page.changed == [] and "since" not in out.typed


async def test_nothing_is_changed_on_a_page_that_asks_for_a_password():
    page = RecencyPage(_rules(), password=True)
    out = await _work(page, phrases=SEARCH.phrases)
    assert page.changed == [] and out.typed is None


async def test_no_title_means_no_typing_and_no_change_of_the_filter():
    page = RecencyPage(_rules())
    out = await _work(page, phrases=(), place="Northtown")
    assert page.changed == [] and out.typed is None and out.before_url is None


async def test_a_filter_that_asks_for_nothing_until_its_form_is_submitted_is_submitted_with_the_forms_own_button():
    page = RecencyPage(_rules(), needs_submit=True)
    out = await _work(page, phrases=SEARCH.phrases)
    assert out.typed["since"]["after"] == "r604800" and list(out.after.values())[-1] == "since"


async def test_a_filter_that_asks_for_nothing_even_when_submitted_is_not_learned():
    page = RecencyPage(_rules(), needs_submit=True)
    original = page._ask
    page._ask = lambda: None if page.values.get("since") else original()  # the change itself leaves no request
    out = await _work(page, phrases=SEARCH.phrases)
    assert "since" not in out.typed and "ask for nothing" in out.skipped["since"]


async def test_a_date_filter_is_set_a_week_back_and_a_number_of_days_to_seven():
    date_box = {**SELECT, "kind": "date", "options": [], "hint": "Posted after"}
    page = RecencyPage(_rules(recency=date_box))
    out = await _work(page, phrases=SEARCH.phrases, today=date(2026, 11, 17))
    assert out.typed["since"] == {"format": "date", "before": "", "after": "2026-11-10"}
    days_box = {**SELECT, "kind": "number", "options": [], "hint": "Posted within (days)"}
    page = RecencyPage(_rules(recency=days_box))
    out = await _work(page, phrases=SEARCH.phrases)
    assert out.typed["since"] == {"format": "days", "before": "", "after": "7"}
    secs_box = {**days_box, "hint": "Max age in seconds"}
    page = RecencyPage(_rules(recency=secs_box))
    assert (await _work(page, phrases=SEARCH.phrases)).typed["since"]["after"] == str(7 * DAY)


async def test_a_number_that_does_not_say_what_it_counts_is_left_alone():
    page = RecencyPage(_rules(recency={**SELECT, "kind": "number", "options": [], "hint": "Posted"}))
    out = await _work(page, phrases=SEARCH.phrases)
    assert page.changed == [] and "since" not in out.typed


async def test_a_refusal_after_the_filter_is_changed_stops_the_recording():
    page = RecencyPage(_rules())
    original = page.request

    def refuse(kind, url, status, mime, body=None):
        refused = "posted=r604800" in url
        original("Document" if refused else kind, url, 403 if refused else status, mime, body)

    page.request = refuse
    out = await _work(page, phrases=SEARCH.phrases)
    assert out.stop == 403 and "next" not in out.done


# --- code finds the field and the recipe is checked against it ---------------------------------------------------


async def _capture() -> dict:
    page = RecencyPage(_rules())
    raw, stop = await recipe_record.capture_page(PAGE, page, search=SEARCH)
    assert stop is None
    return recipes.sanitize(raw, PAGE, NOW)


def _recipe(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": API, "items": "results",
            "params": {"q": "{phrase}", "where": "{place}", "posted": "{since}"},
            "fields": {"title": "title", "url": "path", "ext_id": "jobId"}, "url_prefix": f"https://{HOST}"}
    return {**base, **over}


async def test_the_capture_carries_the_filter_and_the_writer_is_shown_which_request_followed_it():
    capture = await _capture()
    assert capture["typed"]["since"]["after"] == "r604800"
    assert [r["after"] for r in capture["responses"]][-1] == "since"
    text = recipes.trim(capture)
    assert '"search_typed": "since"' in text
    assert '{"typed": ' not in (recipes.strip_typed(text) or "")  # the line that holds the filter goes with it


async def test_code_finds_the_field_that_moved_with_the_filter_by_diffing_the_two_requests():
    capture = await _capture()
    by = {r["after"]: search_fill.leaves(r["url"]) for r in capture["responses"]}
    where = search_fill.blank_places(by["phrase"], by["second"], by["place"], capture["typed"],
                                     last=by["place"], with_since=by["since"])
    assert where["since"] == {("q", "posted")} and where["phrase"] == {("q", "q")}


async def test_a_recipe_that_writes_since_where_the_filter_moved_reproduces_the_recording_and_gets_its_entry_by_code():
    capture = await _capture()
    verdict = await recipes.verify_offline(_recipe(), capture)
    assert verdict.ok, verdict.problems
    made = recipes.with_since_spec(_recipe(since={"format": "date"}), capture, capture["typed"])
    assert made["since"] == {"format": "choices", "choices": CHOICES}  # the writer's own entry is never used


async def test_a_recipe_that_leaves_the_recorded_filter_value_in_its_request_is_asked_to_use_since():
    capture = await _capture()
    verdict = await recipes.verify_offline(_recipe(params={"q": "{phrase}", "where": "{place}",
                                                           "posted": "r604800"}), capture)
    assert not verdict.ok and any("{since}" in p and "posted" in p for p in verdict.problems)


async def test_a_recipe_that_ignores_the_filter_the_recording_changed_is_asked_to_use_since():
    capture = await _capture()
    verdict = await recipes.verify_offline(_recipe(params={"q": "{phrase}", "where": "{place}"}), capture)
    assert not verdict.ok and any("write {since}" in p for p in verdict.problems)


async def test_a_since_blank_on_another_field_than_the_one_that_moved_is_refused():
    capture = await _capture()
    verdict = await recipes.verify_offline(_recipe(params={"q": "{phrase}", "where": "{since}"}), capture)
    assert not verdict.ok and any("put it there" in p for p in verdict.problems)


async def test_a_board_whose_recording_found_no_filter_has_no_since_and_a_recipe_using_it_is_refused():
    page = RecencyPage(_rules(recency=None))
    one = search_fill.Search(("Chef de Partie",), ("Northtown",))
    raw, _ = await recipe_record.capture_page(PAGE, page, search=one)
    capture = recipes.sanitize(raw, PAGE, NOW)
    assert "since" not in capture["typed"]
    plain = await recipes.verify_offline(_recipe(params={"q": "{phrase}", "where": "{place}"}), capture)
    assert plain.ok, plain.problems  # read as it is: no filter, no window
    used = await recipes.verify_offline(_recipe(), capture)
    assert not used.ok and any("no recency filter" in p for p in used.problems)


async def test_the_replay_of_a_recording_fills_since_with_the_recorded_value_exactly():
    capture = await _capture()
    search = recipes.typed_search(capture["typed"], {"phrase", "place", "since"})
    assert search.since_literal == "r604800" and search.phrases == ("Sous Chef",)
    assert recipes.typed_search(capture["typed"], {"phrase", "place"}).since_literal is None


async def test_a_numbers_type_in_the_request_is_taken_from_what_the_page_sent():
    sent = {"method": "POST", "url": API, "post_data": json.dumps({"q": "Chef", "age": 3}), "status": 200,
            "content_type": "application/json", "body": json.dumps({"results": _jobs(3)}), "after": "phrase"}
    changed = {**sent, "post_data": json.dumps({"q": "Chef", "age": 7}), "after": "since"}
    typed = {"phrase": "Chef", "second": None, "place": None, "since": {"format": "days", "before": "3", "after": "7"}}
    capture = recipes.sanitize([sent, changed, {"typed": typed}], PAGE, NOW)
    recipe = {"kind": "request", "method": "POST", "url": API, "items": "results", "body": {"q": "{phrase}",
              "age": "{since}"}, "fields": {"title": "title", "url": "path", "ext_id": "jobId"}}
    assert recipes.with_since_spec(recipe, capture, capture["typed"])["since"] == {"format": "days", "number": True}
    verdict = await recipes.verify_offline(recipe, capture)
    assert verdict.ok, verdict.problems


# --- kept and read ---------------------------------------------------------------------------------------------------

pytestmark_db = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")


async def _recording(capture: dict) -> tuple[SiteRecording, ParseJob, Site]:
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=PAGE, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        rec = await recipe_record.request(session, OWNER, site)
        rec.state, rec.recorded_at, rec.sha256 = "recorded", NOW, recipes.fingerprint(capture)
        rec.body, rec.responses = recipes.pack(capture), len(capture["responses"])
        job = await recipe_record.queue_write(session, OWNER, rec, capture)
        await session.commit()
        return rec, job, site


@pytestmark_db
async def test_a_recipe_is_kept_with_the_entry_the_code_made_and_none_of_the_recorded_values(js):
    capture = await _capture()
    _rec, job, site = await _recording(capture)
    async with db.session_factory()() as session:
        done, problems = await recipe_record.report(session, OWNER, await session.get(ParseJob, job.id),
                                                    {"recipe": _recipe(since={"format": "days"})}, NOW)
        await session.commit()
    assert (done, problems) == ("done", [])
    async with db.session_factory()() as session:
        kept = (await session.scalars(select(SiteRecipe).where(SiteRecipe.site_id == site.id))).one()
    assert kept.recipe["since"] == {"format": "choices", "choices": CHOICES, "number": False}
    assert kept.recipe["params"] == {"q": "{phrase}", "where": "{place}", "posted": "{since}"}


class DatedSite:
    """The made-up site: the recency filter's value of each request it was asked."""

    def __init__(self) -> None:
        self.windows: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.windows.append(request.url.params.get("posted"))
        return httpx.Response(200, json={"results": _jobs(2)})

    def fetcher(self) -> Fetcher:
        return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handle)), per_host_seconds=0)


async def _night(web: DatedSite, monkeypatch, now: datetime) -> dict:
    async def read(session, owner):
        return search_fill.Search(("alpha",), now=now)

    monkeypatch.setattr(search_fill, "read", read)
    async with db.session_factory()() as session:
        fetch = web.fetcher()
        try:
            _built_in, reads = await site_reads.plan(session, OWNER, [])
            counts = await site_reads.refresh(session, OWNER, fetch, reads, now)
            await recipe_live.maintain(session, OWNER, now)
        finally:
            await fetch.close()
        await session.commit()
        return counts


@pytestmark_db
async def test_the_first_night_reads_the_widest_window_and_the_next_one_the_choice_that_covers_the_time_since(
        js, monkeypatch):
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=PAGE, kind="employer", read_by="browser", created_at=NOW - timedelta(days=9))
        session.add(site)
        await session.flush()
        session.add(SiteRecipe(owner=OWNER, site_id=site.id, version=1, state="live", created_at=NOW, recipe={
            **_recipe(params={"q": "{phrase}", "posted": "{since}"}), "since": {"format": "choices",
                                                                                 "choices": CHOICES[1:]}}))
        await session.commit()
    web = DatedSite()
    await _night(web, monkeypatch, NOW)
    assert web.windows == ["r2592000"]  # a first read: the widest choice there is
    await _night(web, monkeypatch, NOW + timedelta(days=1))
    assert web.windows[1:] == ["r604800"]  # a day on: the day and the margin, in a week
    await _night(web, monkeypatch, NOW + timedelta(days=11))
    assert web.windows[2:] == ["r2592000"]  # a missed ten nights are not lost


async def test_a_night_that_went_badly_does_not_move_the_window_forward():
    health = [{"at": NOW.isoformat(), "rows": 4, "requests": 1, "details": 0, "fill": None},
              {"at": (NOW + timedelta(days=1)).isoformat(), "rows": 0, "why": "it read no postings"}]
    assert search_fill.last_good_read(health) == NOW
