"""A recipe with search blanks through the app: written from a recording that says what was typed, kept only when it
holds none of those words, and read each night with the owner's titles as they are then (`jobsearch/browser/recipes/
record.py`, `jobsearch/postings/site_reads.py`). Every site is made up (`*.example.test`); no network, no browser."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import JobPosting, ParseJob
from app.jobsearch.postings import site_reads
from app.sites import recipes, search_fill
from app.sites.models import Site, SiteRecipe, SiteRecording
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
HOST = "careers.example.test"
PAGE = f"https://{HOST}/jobs"
API = f"https://{HOST}/api/search"


def _rows(*ids: int) -> list[dict]:
    return [{"reqId": f"R{i}", "name": f"Role {i}", "link": f"/job/R{i}"} for i in ids]


def _raw(url: str, rows: list[int], after: str) -> dict:
    return {"method": "GET", "url": url, "post_data": None, "status": 200, "content_type": "application/json",
            "body": json.dumps({"results": _rows(*rows)}), "after": after}


def _capture() -> dict:
    return recipes.sanitize([
        _raw(f"{API}?q=Senior+Analyst&kind=job", [1, 2, 3], "phrase"),
        _raw(f"{API}?q=Data+Clerk&kind=job", [4, 5, 6], "second"),
        {"typed": {"phrase": "Senior Analyst", "second": "Data Clerk"}}], PAGE, NOW)


def _recipe(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": API, "items": "results",
            "params": {"q": "{phrase}", "kind": "job"},
            "fields": {"title": "name", "url": "link", "ext_id": "reqId"}, "url_prefix": f"https://{HOST}"}
    return {**base, **over}


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


async def _recipes_of(site: Site) -> list[SiteRecipe]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(SiteRecipe).where(SiteRecipe.site_id == site.id))).all())


async def test_the_writer_is_given_the_typed_words_and_the_keeper_gives_the_job_none_of_them_back(js):
    rec, job, site = await _recording(_capture())
    assert "Senior Analyst" in job.text and '{"typed"' in job.text
    async with db.session_factory()() as session:
        held = await session.get(ParseJob, job.id)
        outcome, problems = await recipe_record.report(session, OWNER, held, {"recipe": _recipe()}, NOW)
        assert (outcome, problems) == ("done", [])
        assert '{"typed"' not in held.text and held.text, "the typed line goes; the rest of the text stays"
        await session.commit()
    [kept] = await _recipes_of(site)
    assert kept.recipe["params"] == {"q": "{phrase}", "kind": "job"}
    assert "Senior" not in json.dumps(kept.recipe) and "Clerk" not in json.dumps(kept.recipe)


async def test_a_recipe_that_fails_every_try_still_leaves_none_of_the_typed_words_in_the_job(js):
    rec, job, site = await _recording(_capture())
    wrong = _recipe(params={"q": "Senior Analyst", "kind": "job"})
    async with db.session_factory()() as session:
        held = await session.get(ParseJob, job.id)
        for tries in (1, 2, 3):
            outcome, problems = await recipe_record.report(session, OWNER, held, {"recipe": wrong}, NOW)
            assert any("still in the request" in p for p in problems)
            assert outcome == ("failed" if tries == 3 else "again")
            assert ('{"typed"' in held.text) == (tries < 3), "the words stay for another try and go with the last"
    assert await _recipes_of(site) == []


async def test_a_recipe_holding_a_typed_word_is_never_kept_even_when_it_is_handed_to_the_keeper(js):
    rec, job, site = await _recording(_capture())
    verdict = recipes.Verdict(True, [], 3, 3)
    async with db.session_factory()() as session:
        held_rec = await session.get(SiteRecording, rec.id)
        with pytest.raises(ValueError, match="words typed in the recording"):
            await recipe_record.keep_recipe(session, OWNER, held_rec, _recipe(params={"q": "data clerk"}), verdict, NOW)
        await recipe_record.keep_recipe(session, OWNER, held_rec, _recipe(), verdict, NOW)
        await session.commit()
    assert len(await _recipes_of(site)) == 1, "only the clean one"


# --- the night's read -------------------------------------------------------------------------------------------------


class SearchSite:
    """The made-up careers site: its search answers each title with rows of its own."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        title = request.url.params["q"]
        self.asked.append(title)
        ids = {"alpha": [1, 2], "beta": [3, 4]}.get(title, [])
        return httpx.Response(200, json={"results": _rows(*ids)})

    def fetcher(self) -> Fetcher:
        return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handle)), per_host_seconds=0)


async def _live() -> SiteRecipe:
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=PAGE, kind="employer", read_by="browser", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        row = SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=_recipe(), state="live", created_at=NOW)
        session.add(row)
        await session.commit()
        return row


async def _night(web: SearchSite, search: search_fill.Search, monkeypatch, now: datetime) -> dict:
    async def read(session, owner):
        return search

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


async def _open_ext_ids() -> list[str]:
    async with db.session_factory()() as session:
        return sorted(p.ext_id for p in (await session.scalars(select(JobPosting))).all() if p.is_open)


async def test_a_changed_title_applies_the_next_night_and_nothing_of_it_is_stored_in_the_recipe(js, monkeypatch):
    row = await _live()
    web = SearchSite()
    first = await _night(web, search_fill.Search(("alpha",)), monkeypatch, NOW)
    assert web.asked == ["alpha"] and first["new"] == 2 and first["failed"] == 0
    second = await _night(web, search_fill.Search(("beta",)), monkeypatch, NOW + timedelta(days=1))
    assert web.asked == ["alpha", "beta"] and second["new"] == 2 and second["failed"] == 0
    assert await _open_ext_ids() == ["R1", "R2", "R3", "R4"], "a search's view closes nothing it did not return"
    async with db.session_factory()() as session:
        assert (await session.get(SiteRecipe, row.id)).recipe["params"] == {"q": "{phrase}", "kind": "job"}


async def test_a_night_with_no_titles_set_leaves_a_blank_recipe_unread_and_is_no_bad_night(js, monkeypatch):
    row = await _live()
    web = SearchSite()
    counts = await _night(web, search_fill.Search(), monkeypatch, NOW)
    assert web.asked == [] and counts["unread"] == 1 and counts["failed"] == 0
    async with db.session_factory()() as session:
        kept = await session.get(SiteRecipe, row.id)
        assert (kept.state, kept.bad_nights) == ("live", 0)


async def test_each_title_of_the_search_is_one_request_in_the_one_nights_read(js, monkeypatch):
    await _live()
    web = SearchSite()
    counts = await _night(web, search_fill.Search(("alpha", "beta", "gamma")), monkeypatch, NOW)
    assert web.asked == ["alpha", "beta", "gamma"] and counts["new"] == 4
