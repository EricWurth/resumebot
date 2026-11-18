"""A site the user added is read by the polite fetcher under its own asks (`sites/added.py`; docs/PRD.md 28.5, 55.12,
the owner, 2026-11-17: "Run it anyway."): robots.txt Disallow does not stop the read, a Crawl-delay and
the pace the site's terms ask for only slow it, and the pace is the strictest of three. Every host is made up
(`example.test`); no network (a mock transport answers)."""

import ast
from pathlib import Path

import httpx
import pytest

from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, RobotsRefused
from app.sites import added
from app.sites.added import Added

HOST = "board.example.test"
OTHER = "other.example.test"
DISALLOW = "User-agent: *\nDisallow: /jobs/private\nDisallow: /search"


def _site(routes: dict, seen: list | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        route = routes.get((request.url.host, request.url.path))
        return httpx.Response(404) if route is None else httpx.Response(route.status_code, text=route.text)
    return handle


def _fetcher(handle, per_host_seconds: float = 0) -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=per_host_seconds)


def _robots(text: str, *hosts: str) -> dict:
    return {**{(h, "/robots.txt"): httpx.Response(200, text=text) for h in hosts},
            **{(h, p): httpx.Response(200, text="ok") for h in hosts for p in ("/jobs/private", "/search", "/jobs")}}


@pytest.fixture(autouse=True)
def nothing_really_waits(monkeypatch):
    """The waits the fetcher asks for are recorded (`slept`), not made."""
    waits: list[float] = []

    async def sleep(seconds, *args):
        waits.append(seconds)
    monkeypatch.setattr(polite.asyncio, "sleep", sleep)
    return waits


async def test_an_added_hosts_disallowed_path_is_read_and_another_hosts_is_still_refused():
    added.replace({HOST: Added()})
    fetch = _fetcher(_site(_robots(DISALLOW, HOST, OTHER)))
    assert (await fetch.get(f"https://{HOST}/jobs/private")).text == "ok"
    assert (await fetch.get(f"https://{HOST}/search?q=a")).text == "ok"
    with pytest.raises(RobotsRefused, match="asks not to read"):
        await fetch.get(f"https://{OTHER}/jobs/private")


async def test_the_added_hosts_robots_txt_is_still_asked_for_its_crawl_delay():
    added.replace({HOST: Added()})
    seen: list = []
    fetch = _fetcher(_site(_robots("User-agent: *\nDisallow: /search\nCrawl-delay: 4", HOST), seen))
    await fetch.get(f"https://{HOST}/search")
    assert "/robots.txt" in [r.url.path for r in seen]
    assert fetch._every(HOST) == 4


async def test_a_crawl_delay_above_thirty_on_an_added_host_is_a_pace_and_not_a_refusal():
    added.replace({HOST: Added()})
    fetch = _fetcher(_site(_robots("User-agent: *\nCrawl-delay: 120", HOST)))
    assert (await fetch.get(f"https://{HOST}/jobs")).text == "ok"
    assert fetch._every(HOST) == 120


async def test_a_crawl_delay_above_the_cap_is_held_to_the_one_cap():
    added.replace({HOST: Added()})
    fetch = _fetcher(_site(_robots("User-agent: *\nCrawl-delay: 99999", HOST)))
    await fetch.get(f"https://{HOST}/jobs")
    assert fetch._every(HOST) == added.MAX_PACE_SECONDS == 600


async def test_the_same_crawl_delay_on_a_host_that_was_not_added_still_leaves_it_alone():
    fetch = _fetcher(_site(_robots("User-agent: *\nCrawl-delay: 120", OTHER)))
    with pytest.raises(RobotsRefused, match="more time between requests"):
        await fetch.get(f"https://{OTHER}/jobs")


@pytest.mark.parametrize(("terms", "crawl", "ours", "expected"), [
    (29, 5, 1, 29),      # the terms ask the most
    (3, 29, 1, 29),      # robots.txt asks the most
    (3, 5, 40, 40),      # our default is the slowest of the three
    (0, 4, 1, 4),        # no ask stated: only Crawl-delay and the default
    (2, 0, 1, 2),        # no 30 second floor: a short ask over our default stands as it is
])
async def test_the_pace_is_the_strictest_of_our_default_the_crawl_delay_and_what_the_terms_ask(
        terms, crawl, ours, expected):
    added.replace({HOST: Added(pace_seconds=terms)})
    text = f"User-agent: *\nCrawl-delay: {crawl}" if crawl else "User-agent: *\nDisallow: /nothing"
    fetch = _fetcher(_site(_robots(text, HOST)), per_host_seconds=ours)
    await fetch.get(f"https://{HOST}/jobs")
    assert fetch._every(HOST) == expected


async def test_no_value_the_overlay_holds_can_lower_a_pace():
    """Whatever the overlay says (a learned pace, a terms ask, a hostile record), the gap is never below the host's
    default pace nor its Crawl-delay."""
    for ask in (0.0, 0.5, -3.0):
        added.replace({HOST: Added(pace_seconds=ask)})
        fetch = _fetcher(_site(_robots("User-agent: *\nCrawl-delay: 7", HOST)), per_host_seconds=2)
        await fetch.get(f"https://{HOST}/jobs")
        assert fetch._every(HOST) == 7


async def test_robots_txt_that_cannot_be_had_does_not_stop_an_added_host_and_still_stops_another():
    added.replace({HOST: Added()})
    routes = {(h, "/robots.txt"): httpx.Response(503) for h in (HOST, OTHER)}
    routes |= {(h, "/jobs"): httpx.Response(200, text="ok") for h in (HOST, OTHER)}
    fetch = _fetcher(_site(routes))
    assert (await fetch.get(f"https://{HOST}/jobs")).text == "ok"
    with pytest.raises(RobotsRefused) as caught:
        await fetch.get(f"https://{OTHER}/jobs")
    assert caught.value.unreachable


async def test_reading_an_added_host_never_goes_through_the_owners_exception():
    """The owner's exception (`read_exempt`, its four guards, the global setting) is a different door: an added host
    leaves it exactly as it was."""
    added.replace({HOST: Added()})
    fetch = _fetcher(_site(_robots(DISALLOW, HOST)))
    await fetch.get(f"https://{HOST}/jobs/private")
    assert fetch._exempt == {}
    assert fetch.read_added(HOST) is True and fetch.read_added(OTHER) is False


def test_a_host_leaves_the_door_at_once_when_the_overlay_is_replaced_without_it():
    added.replace({HOST: Added()})
    fetch = _fetcher(_site({}))
    assert fetch.read_added(HOST) and fetch.read_added(f"WWW.{HOST}.")
    added.replace({})
    assert not fetch.read_added(HOST)


def test_the_door_for_added_hosts_has_no_caller_outside_the_fetcher():
    """`Fetcher.read_added` is the one place Disallow is ignored for an added host; nothing else calls it."""
    app = Path(__file__).resolve().parents[2] / "app"
    callers = []
    for path in app.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "read_added" and path.name != "polite.py":
                callers.append(path.name)
    assert callers == []


def test_the_robots_exempt_door_is_untouched():
    source = Path(polite.__file__).read_text(encoding="utf-8")
    assert source.count("_exempt_robots(") == 2  # its definition and its one call, in `read_exempt`
