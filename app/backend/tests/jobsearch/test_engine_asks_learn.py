"""The engine's ask `site.learn`: a site the owner added to search is learned at once, with no night's slot. Its terms
are read and waited for (success or failure), the site is tested, and one first recording is asked for, which the
browser queue makes an item taken at any hour. Every site is made up (`*.example.test`); nothing here reaches the
network, a model or a browser."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.missions import core as missions
from app.jobsearch.models import BrowserItem, Mission, ParseJob
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Settings
from app.jobsearch.work import engine_asks, engine_calls
from app.sites import added, autostart, terms_read, where, where_view
from app.sites.models import Project, Site, SiteRecording, SiteSkill
from app.timeutil import owner_zone
from tests.support import OWNER, load_profile

URL = "https://board.example.test/jobs"
HOST = "board.example.test"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(engine_asks, "TERMS_POLL_S", 0.0)
    monkeypatch.setattr(engine_asks, "TERMS_WAIT_S", 0.0)
    yield
    added.clear()


async def _site(url: str = URL, *, read_by: str = "app", added_by: str | None = None, **changes) -> uuid.UUID:
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, url, read_by=read_by)
        if added_by:
            site.added_by = added_by
        for name, value in changes.items():
            setattr(site, name, value)
        site_id = site.id
        await session.commit()
    return site_id


async def _mission(session, key: str) -> Mission:
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    await missions.ensure(session, OWNER)
    return await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == key))


async def _learn(site_id: uuid.UUID) -> dict:
    async with db.session_factory()() as session:
        answer = await engine_asks._asks["site.learn"](session, OWNER, {"site_id": str(site_id)})
        await session.commit()
    return answer


async def _recordings(site_id: uuid.UUID) -> list[SiteRecording]:
    async with db.read_session_factory()() as reader:
        return list((await reader.scalars(select(SiteRecording).where(SiteRecording.site_id == site_id))).all())


async def test_an_owner_added_site_gets_one_first_recording_and_one_item_taken_at_any_hour(js):
    site_id = await _site()
    assert await _learn(site_id) == {"learning": True, "why": None}
    [recording] = await _recordings(site_id)
    assert (recording.reason, recording.state) == ("first", "requested")
    async with db.session_factory()() as session:
        [ask] = await recipe_record.wanted(session, OWNER)
        assert (ask["site"], ask["priority"]) == (HOST, 0)
        browser = await _mission(session, missions.BROWSER)
        await missions.resume(session, browser, by="Tester")
        await browser_queue.refresh(session, OWNER)
        await session.commit()
        items = (await session.scalars(select(BrowserItem).where(BrowserItem.kind == bp.RECORD))).all()
    assert [(i.target, i.priority) for i in items] == [(HOST, 0)]


async def test_a_second_ask_for_the_same_site_makes_no_second_recording(js):
    site_id = await _site()
    first = await _learn(site_id)
    second = await _learn(site_id)
    assert first["learning"] is True and second == {"learning": False, "why": "recording"}
    assert len(await _recordings(site_id)) == 1


async def test_two_asks_at_once_make_one_recording(js):
    site_id = await _site()
    answers = await asyncio.gather(_learn(site_id), _learn(site_id))
    assert sorted(a["learning"] for a in answers) == [False, True]
    assert len(await _recordings(site_id)) == 1


@pytest.mark.parametrize("how", ["avoid", "app_added", "off", "own_browser", "skill", "project"])
async def test_a_site_that_is_not_for_learning_gets_no_recording(js, how):
    if how == "avoid":
        site_id = await _site(list_name="avoid")
    elif how == "app_added":
        site_id = await _site(added_by=where.APP_ADDED)
    elif how == "off":
        site_id = await _site(on=False)
    elif how == "own_browser":
        site_id = await _site(read_by="browser")
    else:
        site_id = await _site()
        async with db.session_factory()() as session:
            if how == "skill":
                session.add(SiteSkill(owner=OWNER, site_id=site_id, version=1, created_at=datetime.now(UTC),
                                      recipe={"kind": "request"}))
            else:
                session.add(Project(owner=OWNER, site_id=site_id, title="Read it", outcome="Read it",
                                    created_by="you"))
            await session.commit()
    assert (await _learn(site_id))["learning"] is False
    assert await _recordings(site_id) == []


async def test_a_site_with_an_adapter_reader_gets_no_recording(js, monkeypatch):
    async def system(session, site):
        return "example-system"

    monkeypatch.setattr(where, "adapter_system", system)
    site_id = await _site()
    assert await _learn(site_id) == {"learning": False, "why": "adapter"}
    assert await _recordings(site_id) == []


async def test_a_terms_page_that_cannot_be_read_leaves_the_defaults_and_the_row_says_so(js):
    site_id = await _site()
    await _learn(site_id)
    async with db.read_session_factory()() as reader:
        site = await reader.get(Site, site_id)
        [row] = await where_view.listing(reader, OWNER, site_id)
    assert site is not None and site.polite.get("defaults")
    assert "app's own pace and hours" in row["read_note"]


async def test_the_first_recording_waits_for_the_terms_job_and_a_failed_one_leaves_the_defaults(js, monkeypatch):
    seen: dict = {}

    async def ask(owner, site_id, fetch=None, now=None):
        async with db.session_factory()() as session:  # what `terms_read.ask` leaves: a waiting job, the row asked
            site = await session.get(Site, site_id)
            page = terms_read.Page("https://board.example.test/terms", "Please be gentle.")
            seen["job"] = (await terms_read.queue(session, owner, site, page, datetime.now(UTC))).id
            await session.commit()
        return "queued"

    knocked: list[tuple[str, str]] = []

    async def knock(run_id, reason="x"):
        knocked.append((run_id, reason))
        async with db.session_factory()() as session:  # the parse session fails it
            job = await session.get(ParseJob, uuid.UUID(run_id))
            job.state, job.error = "failed", "the session ended"
            await session.commit()
        return True

    monkeypatch.setattr(terms_read, "ask", ask)
    monkeypatch.setattr(engine_calls, "knock", knock)
    monkeypatch.setattr(engine_asks, "TERMS_WAIT_S", 60.0)
    site_id = await _site()
    assert (await _learn(site_id))["learning"] is True
    assert knocked == [(str(seen["job"]), engine_calls.PARSE_ASKED)]
    async with db.read_session_factory()() as reader:
        site = await reader.get(Site, site_id)
    assert site is not None and site.polite.get("defaults") == "the session ended"
    assert len(await _recordings(site_id)) == 1


async def test_a_recording_waits_while_the_sites_terms_ask_not_to_be_read(js):
    site_id = await _site()
    await _learn(site_id)
    hour = datetime.now(UTC).astimezone(owner_zone()).hour
    added.replace({HOST: added.Added(avoid_hours=((hour, hour + 1),))})
    async with db.session_factory()() as session:
        assert await recipe_record.wanted(session, OWNER) == []
    added.clear()
    async with db.session_factory()() as session:
        assert len(await recipe_record.wanted(session, OWNER)) == 1


async def test_a_recording_that_failed_is_no_failed_browser_learning_and_the_row_says_the_site_stays(js):
    site_id = await _site()
    await _learn(site_id)
    async with db.session_factory()() as session:
        await recipe_record.give_up(session, OWNER, HOST, "no data request carried the listing")
        await session.commit()
    async with db.read_session_factory()() as reader:
        site = await reader.get(Site, site_id)
        [row] = await where_view.listing(reader, OWNER, site_id)
    assert site is not None and (site.learn_tries, site.on) == (0, True)
    assert "Couldn't learn how to read it yet (no data request carried the listing)" in row["read_note"]
    assert "stays listed" in row["read_note"] and row["on"] is True


async def test_where_to_look_says_it_is_learning_while_a_recording_is_open_and_not_before_or_after(js):
    site_id = await _site()
    async with db.read_session_factory()() as reader:
        [before] = await where_view.listing(reader, OWNER, site_id)
    assert before["status"] != where_view.LEARNING_NOW
    await _learn(site_id)
    async with db.read_session_factory()() as reader:
        [during] = await where_view.listing(reader, OWNER, site_id)
    assert during["status"] == "Learning how to read it now"
    async with db.session_factory()() as session:
        await recipe_record.give_up(session, OWNER, HOST, "nothing")
        await session.commit()
    async with db.read_session_factory()() as reader:
        [after] = await where_view.listing(reader, OWNER, site_id)
    assert after["status"] != where_view.LEARNING_NOW


async def test_a_recording_takes_none_of_the_nights_project_slots(js):
    sites = [await _site(f"https://board{i}.example.test/jobs") for i in range(6)]
    await _learn(sites[0])  # learned at once: it has a request open, and is not a project
    async with db.session_factory()() as session:
        made = await autostart.ensure_projects(session, OWNER, limit=Settings().site_projects_per_night, by_switch=True)
        assert len(made) == 5, "the default night still starts five projects"


async def test_the_default_is_five_a_night_and_a_stored_three_stays_three(js):
    assert Settings().site_projects_per_night == 5
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "settings", {"site_projects_per_night": 3}, by="test")
        await session.commit()
    async with db.read_session_factory()() as reader:
        assert (await profile_store.current(reader, OWNER)).settings.site_projects_per_night == 3


async def test_a_site_with_a_recording_open_is_given_no_project_the_same_night(js):
    learned = await _site("https://board-a.example.test/jobs")
    other = await _site("https://board-b.example.test/jobs")
    await _learn(learned)
    async with db.session_factory()() as session:
        made = await autostart.ensure_projects(session, OWNER, limit=5, by_switch=True)
        await session.commit()
    sites = {p.site_id for p in made}
    assert learned not in sites and other in sites
