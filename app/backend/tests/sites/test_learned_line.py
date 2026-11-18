"""The one line that says what was learned of an added site (docs/PRD.md 37.17, item G), on the site's status in Where
to look: how it is read, which of the title, the place and the date it fills, its pace and where that came from, and the
stage that failed and why. Code writes it from the stored recipe and the site's record; no model. Every host is made up
(example.test); no network."""

from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.sites import learned_line, where_view
from app.sites.models import Site, SiteRecipe, SiteRecording
from tests.support import DEFAULT_OWNER as OWNER

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
HOST = "board.example.test"
FIELDS = {"title": "t"}


def _request(**over) -> dict:
    return {"kind": "request", "method": "GET", "url": f"https://api.{HOST}/search?q={{phrase}}&where={{place}}",
            "items": "jobs", "fields": FIELDS, **over}


@pytest.mark.parametrize("recipe, how", [
    (_request(), "Read by a data request"),
    ({**_request(), "kind": "embedded"}, "Read by the data the page carries"),
    ({"kind": "html", "url": f"https://{HOST}/", "search_url": f"https://{HOST}/s?q={{phrase}}", "links": "a"},
     "Read by small HTML from the page"),
    ({"kind": "html", "url": f"https://{HOST}/", "fragment_url": f"https://{HOST}/f?q={{phrase}}&start={{offset}}",
      "links": "a"}, "Read by small HTML from the page"),
    ({"kind": "page", "url": f"https://{HOST}/", "search": "input.q", "links": "a"}, "Read by the app's Chrome"),
    ({"kind": "browser", "start_url": f"https://{HOST}/", "search": {"how": "none"}}, "Read by a browser session"),
    ({"kind": "list_page", "url": f"https://{HOST}/", "link_pattern": "x"}, "Read by the page's links"),
])
def test_the_line_says_how_each_kind_of_recipe_is_read(recipe, how):
    said = learned_line.line(recipe, None)
    assert said is not None and said.startswith(how) and said.endswith(".") and "\n" not in said


def test_the_line_names_the_title_place_and_date_a_recipe_fills():
    both = learned_line.line(_request(), None)
    assert both is not None and "fills title and place" in both and "place unsupported" not in both
    dated = learned_line.line(_request(url=f"https://api.{HOST}/s?q={{phrase}}&w={{place}}&d={{since}}"), None)
    assert dated is not None and "fills title, place and date" in dated
    nothing = learned_line.line(_request(url=f"https://api.{HOST}/s"), None)
    assert nothing is not None and "fills none of your search words" in nothing


def test_a_board_with_no_place_in_its_search_says_place_unsupported():
    said = learned_line.line(_request(url=f"https://api.{HOST}/s?q={{phrase}}"), None)
    assert said is not None and "fills title; place unsupported" in said
    page = learned_line.line({"kind": "page", "url": f"https://{HOST}/", "search": "input.q", "links": "a"}, None)
    assert page is not None and "place unsupported" in page
    box = learned_line.line({"kind": "page", "url": f"https://{HOST}/", "search": "input.q", "place": "input.w",
                             "links": "a"}, None)
    assert box is not None and "fills title and place" in box


def test_the_pace_says_where_it_came_from_and_the_slowest_wins():
    assert "about one request a second (default)" in learned_line.line(_request(), None)
    assert "one request every 29 seconds (terms)" in learned_line.line(_request(), {"rate_seconds": 29})
    assert "every 464 seconds (learned)" in learned_line.line(_request(), {"rate_seconds": 29, "learned_pace": 464})
    assert "every 40 seconds (learned)" in learned_line.line(_request(pace_seconds=40), {"rate_seconds": 29})
    assert "every 600 seconds (terms)" in learned_line.line(_request(), {"rate_seconds": 9000})
    assert "(default)" in learned_line.line(_request(), {"rate_seconds": "fast", "learned_pace": True})


def test_a_failed_stage_is_named_with_its_reason_and_the_line_ends_in_one_full_stop():
    failed = learned_line.Failure("page's HTML", "it asks for a sign-in")
    said = learned_line.line(_request(), None, failed)
    assert said is not None and said.endswith("; the page's HTML stage failed: it asks for a sign-in.")
    alone = learned_line.line(None, None, learned_line.Failure("app's Chrome", "no results page"))
    assert alone == "The app's Chrome stage failed: no results page."
    assert learned_line.line(None, None) is None
    assert learned_line.line({"kind": "adapter", "ats": "x", "board": {}}, None) is None


async def _board(session, path: str = "jobs", **over) -> Site:
    site = Site(owner=OWNER, url=f"https://{HOST}/{path}", kind="job_board", list_name="search", added_by="owner",
                created_at=NOW, **over)
    session.add(site)
    await session.flush()
    return site


def _recording(site: Site, reason: str, state: str, note: str | None, minutes: int) -> SiteRecording:
    return SiteRecording(owner=OWNER, site_id=site.id, reason=reason, state=state, note=note, url=site.url,
                         requested_at=NOW + timedelta(minutes=minutes))


async def test_the_status_of_an_added_site_carries_the_line_after_its_own_words(js):
    async with db.session_factory()() as session:
        site = await _board(session, polite={"rate_seconds": 29})
        session.add_all([_recording(site, "first", "done", None, 0), SiteRecipe(
            owner=OWNER, site_id=site.id, version=1, recipe=_request(), state="live", created_at=NOW)])
        await session.flush()
        [row] = await where_view.listing(session, OWNER, site.id)
    assert row["status"].endswith(
        "Read by a data request; fills title and place; paced at one request every 29 seconds (terms).")
    assert row["status"].index("Read by") > 0, "the status's own words come first"


async def test_a_failed_stage_from_the_recordings_is_said_and_a_fresh_good_learn_clears_it(js):
    async with db.session_factory()() as session:
        site = await _board(session)
        session.add_all([
            _recording(site, "first", "done", None, 0),
            _recording(site, "demote", "done", "the first read of the data recipe was refused (403)", 1),
            _recording(site, "chain", "failed", "the page shows a sign-in wall", 2)])
        await session.flush()
        [row] = await where_view.listing(session, OWNER, site.id)
        assert "The app's Chrome stage failed: the page shows a sign-in wall." in row["status"]
        session.add(_recording(site, "page", "done", None, 3))
        await session.flush()
        [again] = await where_view.listing(session, OWNER, site.id)
    assert "stage failed" not in again["status"]


async def test_a_data_request_that_was_refused_is_named_as_the_stage_that_failed(js):
    async with db.session_factory()() as session:
        site = await _board(session)
        session.add(_recording(site, "demote", "done", "the first read of the data recipe was refused (403)", 1))
        await session.flush()
        [row] = await where_view.listing(session, OWNER, site.id)
    assert "The data request stage failed: was refused (403)." in row["status"]


async def test_a_site_the_owner_did_not_add_or_that_is_off_has_no_line(js):
    async with db.session_factory()() as session:
        app_made = await _board(session)
        app_made.added_by = "app"
        off = await _board(session, "off")
        off.on = False
        for site in (app_made, off):
            session.add(_recording(site, "first", "done", None, 0))
            session.add(SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=_request(), state="live",
                                   created_at=NOW))
        await session.flush()
        rows = await where_view.listing(session, OWNER)
    assert all("Read by" not in r["status"] for r in rows)
