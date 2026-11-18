"""The overlay of the sites the user added (`sites/added.py`): who enters it and who never does, that a runbook wins and
a runbook that did not load keeps its hosts closed, that a refusal on an added host stops it until the next off hours
and not for good (docs/PRD.md 28.5, 55.12), and that a rebuild neither extends nor clears a stop. Keyed on the host,
never on a name. Every host is made up (`example.test`); no network."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.fetch import host_stops
from app.jobsearch.models import BrowserDomain
from app.sites import added, registry
from app.sites.added import Added
from app.sites.models import Site
from app.sites.registry import Problem, Registry
from app.sites.schema import Runbook
from app.timeutil import utc_now
from tests.support import DEFAULT_OWNER as OWNER

HOST = "board.example.test"
BY_DAY = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)  # the made-up owner's off hours are 1 to 7 AM
TONIGHT = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)


def _row(url: str = f"https://{HOST}/jobs", **over) -> SimpleNamespace:
    row = {"url": url, "added_by": "owner", "list_name": "search", "on": True, "polite": None}
    return SimpleNamespace(**{**row, **over})


def _registry(*docs: dict, problems: tuple[Problem, ...] = ()) -> Registry:
    return Registry([Runbook.model_validate(d) for d in docs], problems, restricted_present=True, terms=())


def _doc(key: str, host: str, **over) -> dict:
    return {"schema": 1, "key": key, "name": key.title(), "kind": "board", "hosts": [host], **over}


@pytest.fixture
def plain():
    with registry.replaced(_registry(_doc("known", "known.example.test"))) as reg:
        host_stops.reset()
        yield reg
        host_stops.reset()


# --- who enters ---------------------------------------------------------------------------------------------------


def test_a_site_the_owner_added_enters_keyed_on_its_host(plain):
    entries = added.entries_of([_row(f"https://www.{HOST}/jobs?x=1")], plain)
    assert list(entries) == [HOST]
    assert added.get(HOST) is None, "building entries changes nothing until they are made the overlay"


@pytest.mark.parametrize("over", [
    {"added_by": "app"},        # a site the app made for an employer, or found
    {"list_name": "avoid"},     # a site to avoid is never read
    {"on": False},              # switched off
], ids=["app-added", "avoid", "off"])
def test_a_row_that_is_not_an_owners_site_to_search_that_is_on_never_enters(plain, over):
    assert added.entries_of([_row(**over)], plain) == {}


def test_a_removed_site_is_simply_not_among_the_rows(plain):
    assert added.entries_of([_row()], plain) and added.entries_of([], plain) == {}


def test_a_hosts_runbook_wins_over_the_overlay(plain):
    entries = added.entries_of([_row("https://known.example.test/jobs"), _row()], plain)
    assert list(entries) == [HOST], "the runbook's host is left to the runbook"


def test_a_host_whose_runbook_did_not_load_stays_closed(plain):
    broken = _registry(_doc("known", "known.example.test"), problems=(Problem("x.json", "not JSON", (HOST,)),))
    assert added.entries_of([_row()], broken) == {}
    with registry.replaced(broken):
        added.replace({HOST: Added()})  # even were it in the overlay, the registry refuses it first
        with pytest.raises(registry.RunbookUnavailable):
            registry.ends_with_the_night(HOST)
        assert "did not load" in (host_stops.stopped_reason(HOST) or "")


def test_two_rows_of_one_host_take_the_slower_pace_and_both_rows_hours(plain):
    one = _row(polite={"rate_seconds": 4, "avoid_hours": [[9, 11]]})
    two = _row(f"https://{HOST}/other", polite={"rate_seconds": 20, "avoid_hours": [[9, 11], [13, 14]]})
    assert added.entries_of([one, two], plain)[HOST] == Added(20.0, ((9, 11), (13, 14)))


@pytest.mark.parametrize(("polite", "pace"), [
    ({"rate_seconds": 29}, 29.0), ({"rate_seconds": 5000}, 600.0), ({"rate_seconds": 0}, 0.0),
    ({"rate_seconds": -4}, 0.0), ({"rate_seconds": "fast"}, 0.0), ({"rate_seconds": True}, 0.0),
    ({"rate_seconds": float("inf")}, 0.0), (None, 0.0), ("text", 0.0), ({}, 0.0)])
def test_the_pace_a_record_asks_for_is_clamped_and_only_ever_a_number_above_zero(plain, polite, pace):
    assert added.entries_of([_row(polite=polite)], plain)[HOST].pace_seconds == pace


@pytest.mark.parametrize(("hours", "kept"), [
    ([[9, 11]], ((9, 11),)), ([[9, 11, 3], [25, 3], ["a", 2], [1.5, 2], "x"], ()), (None, ()), ("x", ())])
def test_hours_to_avoid_that_are_not_whole_hours_of_the_day_are_left_out(plain, hours, kept):
    assert added.entries_of([_row(polite={"avoid_hours": hours})], plain)[HOST].avoid_hours == kept


# --- the rows, and the datagram -----------------------------------------------------------------------------------


async def _site(session, url: str = f"https://{HOST}/jobs", **over) -> Site:
    fields = {"added_by": "owner", "list_name": "search", "on": True, **over}
    row = Site(owner=OWNER, url=url, kind="job_board", created_at=utc_now(), **fields)
    session.add(row)
    await session.commit()
    return row


async def test_a_rebuild_reads_the_owners_rows_and_a_switched_off_site_leaves_at_once(js, plain):
    async with db.session_factory()() as session:
        row = await _site(session, polite={"rate_seconds": 12})
        await _site(session, "https://other.example.test/x", added_by="app")
    async with db.read_session_factory()() as reader:
        assert await added.rebuild(reader, OWNER) == 1
    assert added.hosts() == (HOST,) and added.pace(HOST) == 12.0
    async with db.session_factory()() as session:
        (await session.get(Site, row.id)).on = False
        await session.commit()
    async with db.read_session_factory()() as reader:
        await added.rebuild(reader, OWNER)
    assert added.hosts() == ()


async def test_a_datagram_that_may_mean_a_site_changed_makes_the_overlay_again(js, plain):
    async with db.session_factory()() as session:
        await _site(session)
    assert added.hosts() == ()
    added.heard(SimpleNamespace(bell="look", items=[]))
    await added.settled()
    assert added.hosts() == (HOST,)


async def test_a_page_bound_notice_of_a_site_or_a_resync_does_too_and_any_other_does_not(js, plain):
    async with db.session_factory()() as session:
        await _site(session)
    added.heard(SimpleNamespace(bell=None, items=[("role", "1", [])]))
    await added.settled()
    assert added.hosts() == ()
    for kind in ("site", "resync"):
        added.clear()
        added.heard(SimpleNamespace(bell=None, items=[(kind, "1", [])]))
        await added.settled()
        assert added.hosts() == (HOST,), kind


async def test_a_rebuild_that_fails_leaves_the_overlay_as_it_was(js, plain, monkeypatch):
    added.replace({HOST: Added(7.0)})

    async def broken(session, owner):
        raise RuntimeError("the file is busy")

    monkeypatch.setattr(added, "rebuild", broken)
    assert await added.reload() == 1 and added.pace(HOST) == 7.0


# --- the stops ------------------------------------------------------------------------------------------------------


def test_a_refusal_on_an_added_host_ends_with_the_night_and_on_an_unknown_host_is_for_good(plain):
    added.replace({HOST: Added()})
    assert registry.ends_with_the_night(HOST) is True
    assert host_stops.ends_with_the_night(HOST) is True
    assert host_stops.ends_with_the_night(f"www.{HOST}") is True
    assert host_stops.stop_key(f"www.{HOST}") == HOST
    assert host_stops.ends_with_the_night("elsewhere.example.test") is False, "not a site: stopped for good"
    assert host_stops.ends_with_the_night(f"jobs.{HOST}") is False, "only the host that was added, not its siblings"


def test_a_host_a_runbook_covers_keeps_its_runbooks_stop_not_the_overlays(plain):
    added.replace({"known.example.test": Added()})
    assert registry.ends_with_the_night("known.example.test") is False, "the runbook says for good; it wins"


async def _stopped_until(host: str):
    async with db.session_factory()() as session:
        return await session.scalar(select(BrowserDomain.stopped_until).where(
            BrowserDomain.owner == OWNER, BrowserDomain.domain == host))


@pytest.mark.parametrize("answered", [403, 401, 429])
async def test_a_refusal_stops_an_added_host_until_the_next_off_hours_and_not_for_good(js, plain, answered):
    added.replace({HOST: Added()})
    host_stops._stopped = {}
    await host_stops.record_fetch_refusal(HOST, answered)
    assert host_stops.stopped_reason(HOST) and host_stops._ends[HOST] > utc_now()
    assert await _stopped_until(HOST) is not None, "kept with an end, never for good"


async def test_a_bot_check_and_a_plain_refusal_stop_an_added_host_until_the_next_off_hours(js, plain):
    other = "second.example.test"
    added.replace({HOST: Added(), other: Added()})
    host_stops._stopped = {}
    async with db.session_factory()() as session:
        first = await host_stops.stop(session, OWNER, HOST, BY_DAY, "a Cloudflare bot check")
        second = await host_stops.stop(session, OWNER, other, BY_DAY, "the site refused automated readers")
        await session.commit()
    assert first.stopped_until == TONIGHT and second.stopped_until == TONIGHT


async def test_a_host_that_is_no_site_is_still_stopped_for_good(js, plain):
    host_stops._stopped = {}
    async with db.session_factory()() as session:
        pace = await host_stops.stop(session, OWNER, "elsewhere.example.test", BY_DAY, "a bot check")
        await session.commit()
    assert pace.stopped_until is None


async def test_a_rebuild_neither_extends_nor_clears_a_stop(js, plain):
    added.replace({HOST: Added()})
    host_stops._stopped = {}
    async with db.session_factory()() as session:
        await host_stops.stop(session, OWNER, HOST, BY_DAY, "a bot check")
        await session.commit()
    end, why = host_stops._ends[HOST], host_stops._stopped[HOST]
    for entries in ({}, {HOST: Added(30.0)}, {}):  # the site leaves, comes back, leaves
        added.replace(entries)
        assert host_stops.stopped_reason(HOST, BY_DAY) == why
        assert host_stops._ends[HOST] == end
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER, BY_DAY)  # a restart finds the same stop in the record
    assert host_stops._ends[HOST] == end and host_stops.ends_with_the_night(HOST) is True


async def test_a_page_switch_off_or_removal_rings_a_look_bell_that_rebuilds_the_overlay(
        client, fake_starts, monkeypatch):
    from tests.support import OWNER as CLIENT_OWNER

    rung: list[str] = []
    monkeypatch.setattr(fake_starts, "nudge", lambda: rung.append("look"))
    async with db.session_factory()() as session:
        kept = Site(owner=CLIENT_OWNER, url=f"https://{HOST}/jobs", kind="job_board", added_by="owner",
                    list_name="search", on=True, created_at=utc_now())
        avoided = Site(owner=CLIENT_OWNER, url="https://avoid.example.test/", kind="job_board", added_by="owner",
                       list_name="avoid", on=False, created_at=utc_now())
        session.add_all([kept, avoided])
        await session.commit()
    off = await client.patch(f"/api/v1/sites/{kept.id}", json={"on": False})
    assert off.status_code == 200 and rung  # the watched `on` column rings a look bell at the commit
    rung.clear()
    gone = await client.delete(f"/api/v1/sites/{avoided.id}")
    assert gone.status_code == 200 and rung  # a removed row rings one too
