"""A learned search proves it returns the roles the site showed (PRD 37.17). Offline: the recipe replayed on a
recording that holds the page's visible cards reads those roles. Live: on the first read after an add, a second title
reads roles too and not the first title's list. Every site is made up (`*.example.test`); no network, model or browser:
the fetcher is `httpx.MockTransport` and the recording is built by hand."""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from app import db
from app.jobsearch.browser.recipes import html_record
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.models import ParseJob
from app.sites import recipes, same_roles, search_fill
from app.sites.models import SiteRecipe, SiteRecording
from tests.jobsearch.test_board_chain import (  # noqa: F401  (quick is the autouse fixture those tests run under)
    API_RECIPE,
    HOST,
    SEARCH,
    Web,
    _added,
    _api,
    _live,
    _recipes_of,
    quick,
)
from tests.support import OWNER

URL = f"https://{HOST}/openings"
RECIPE = {"kind": "request", "method": "GET", "url": f"https://{HOST}/api/search", "items": "results",
          "params": {"q": "{phrase}"}, "fields": {"title": "name", "url": "link", "ext_id": "reqId", "company": "org"},
          "url_prefix": f"https://{HOST}"}
NOW = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)


def _answer(n: int = 5) -> str:
    return json.dumps({"results": [{"reqId": f"J{i}", "name": f"Chef de partie {i}", "link": f"/job/J{i}",
                                    "org": f"Kitchen {i}"} for i in range(1, n + 1)]})


def _capture(cards: list[dict] | None) -> dict:
    raw: list[dict] = [{"method": "GET", "url": f"https://{HOST}/api/search?q=Chef", "post_data": None, "status": 200,
                        "content_type": "application/json", "body": _answer(), "after": "phrase"},
                       {"typed": {"phrase": "Chef"}}]
    if cards is not None:
        raw.append({"cards": cards})
    return recipes.sanitize(raw, URL)


def _card(i: int, **over) -> dict:
    return {"id": f"J{i}", "title": f"Chef de partie {i}", "company": f"Kitchen {i}", **over}


def _other(n: int) -> list[dict]:
    return [{"id": f"X{i}", "title": f"Dishwasher {i}", "company": "Elsewhere"} for i in range(1, n + 1)]


# --- offline -------------------------------------------------------------------------------------


async def test_a_recipe_that_reads_the_roles_the_page_showed_is_kept():
    verdict = await recipes.verify_offline(RECIPE, _capture([_card(i) for i in range(1, 6)]))
    assert verdict.ok, verdict.problems


async def test_a_recipe_that_reads_other_roles_than_the_cards_showed_is_not_kept():
    verdict = await recipes.verify_offline(RECIPE, _capture(_other(5)))
    assert not verdict.ok
    assert any(p.startswith(same_roles.PROBLEM) for p in verdict.problems)


async def test_roles_are_matched_by_title_when_the_cards_carry_no_id_or_company():
    cards = [{"id": None, "title": f"CHEF de Partie {i}", "company": None} for i in range(1, 6)]
    assert (await recipes.verify_offline(RECIPE, _capture(cards))).ok


async def test_the_tolerance_is_a_share_of_the_cards_not_every_card():
    assert (await recipes.verify_offline(RECIPE, _capture([_card(i) for i in range(1, 6)] + _other(1)))).ok  # 5 of 6
    assert not (await recipes.verify_offline(RECIPE, _capture([_card(i) for i in range(1, 4)] + _other(3)))).ok  # 3/6


async def test_a_recording_with_no_cards_or_too_few_is_not_held_to_the_check():
    assert (await recipes.verify_offline(RECIPE, _capture(None))).ok
    assert (await recipes.verify_offline(RECIPE, _capture(_other(2)))).ok


def test_the_cards_are_kept_in_the_capture_bounded_and_cleaned():
    capture = _capture([_card(i) for i in range(1, 100)] + [{"title": ""}, "junk"])  # type: ignore[list-item]
    assert len(capture["cards"]) == same_roles.CARDS_MAX
    assert set(capture["cards"][0]) == {"id", "title", "company"}


def test_the_cards_a_rendered_results_page_shows_are_read_by_code():
    items = "".join(f'<li class="res"><a href="/job/{i}">Pastry chef number {i}</a></li>' for i in range(1, 6))
    cards = html_record.cards_of(f"<html><body><ul>{items}</ul></body></html>", URL)
    assert [c["title"] for c in cards] == [f"Pastry chef number {i}" for i in range(1, 6)]
    assert html_record.cards_of("<html><body><p>No jobs</p></body></html>", URL) == []


async def test_a_recipe_that_never_reads_the_cards_roles_goes_on_to_the_pages_html_after_its_tries(js):
    site = await _added(body=_capture(_other(5)))
    async with db.session_factory()() as session:
        rec = (await session.scalars(select(SiteRecording).where(SiteRecording.site_id == site.id))).one()
        rec.state, rec.recorded_at = "recorded", NOW
        job = ParseJob(owner=OWNER, kind="recipe", role_id=f"recipe:{rec.id}", text="x", state="waiting",
                       created_at=NOW, fields={"recording_id": str(rec.id), "site_id": str(site.id),
                                               "host": rec.host, "posting": False, "tries": 2})
        session.add(job)
        await session.flush()
        word, problems = await recipe_record.report(session, OWNER, job, {"recipe": RECIPE}, NOW)
        assert (word, problems) == ("done", [])
        assert (rec.state, rec.body is not None) == ("requested", True), "the html turn takes it next"
        assert "roles the page showed" in (rec.note or "")
        await session.commit()
    assert await _recipes_of(site) == []


# --- live ----------------------------------------------------------------------------------------


async def _row(site) -> SiteRecipe:
    [row] = await _recipes_of(site)
    return row


async def test_a_second_title_that_reads_roles_of_its_own_leaves_the_recipe_live(js, monkeypatch):
    site = await _added()
    await _live(site, API_RECIPE)
    Web(_api()).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "ok"
    assert (await _row(site)).state == "live"


async def test_a_second_title_that_returns_the_first_titles_list_marks_the_recipe_suspect_and_asks_a_re_record(
        js, monkeypatch):
    site = await _added()
    await _live(site, API_RECIPE)

    def same(_request):
        return 200, json.dumps({"results": [{"reqId": f"R{i}", "name": f"Role {i}", "link": f"/job/R{i}"}
                                            for i in range(1, 4)]}), "application/json"

    web = Web(same).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "suspect"
    assert len(web.asked) == 2
    assert (await _row(site)).state == "stale", "a stale recipe is not live: the browser reads the site meanwhile"
    async with db.session_factory()() as session:
        asked = (await session.scalars(select(SiteRecording).where(SiteRecording.reason == "repair"))).all()
    assert [r.state for r in asked] == ["requested"], "the existing stale path asks the re-record"


async def test_a_second_title_that_returns_no_roles_marks_the_recipe_suspect(js, monkeypatch):
    site = await _added()
    await _live(site, API_RECIPE)

    def second_empty(request):
        if request.url.params.get("q") == "Chef":
            return _api()(request)
        return 200, json.dumps({"results": []}), "application/json"

    Web(second_empty).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "suspect"
    assert (await _row(site)).state == "stale"


async def test_one_title_only_skips_the_second_check(js, monkeypatch):
    site = await _added()
    await _live(site, API_RECIPE)

    async def one(_session, _owner):
        return search_fill.Search(("Chef",), SEARCH.places)

    monkeypatch.setattr(search_fill, "read", one)

    def same(_request):
        return 200, json.dumps({"results": [{"reqId": "R1", "name": "Role 1", "link": "/job/R1"}]}), (
            "application/json")

    web = Web(same).serve(monkeypatch)
    assert await recipe_live.first_read(OWNER, site.id) == "ok"
    assert len(web.asked) == 1
    assert (await _row(site)).state == "live"


def test_the_same_list_in_another_order_is_the_same_list_and_a_different_one_is_not():
    a = [{"ext_id": "1", "title": "A"}, {"ext_id": "2", "title": "B"}]
    assert same_roles.same_list(a, list(reversed(a)))
    assert not same_roles.same_list(a, [{"ext_id": "3", "title": "C"}])
    assert same_roles.same_list([{"title": "Cook", "company": "X"}], [{"title": "cook ", "company": "x"}])
