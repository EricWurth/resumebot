"""The sites the user added, as the polite layer sees them: an overlay, host to what that host asks of a reader.

A site the user adds is read after the terms warning (the owner, 2026-11-17: "Don't add switches. If they add the site.
Show the warning. Run it anyway."). robots.txt Disallow and a terms clause against automated reading do not stop that
read, but the site's own asks slow it: its robots.txt Crawl-delay and the pace its terms ask for (`js_sites.polite`),
whichever is strictest, never lower than the app's default. A refusal of any kind (401, 403, 429, a bot check) stops
the host until the owner's next off hours, not for good (`registry.ends_with_the_night`).

This module holds what the fetcher and the registry ask, per host, from memory. The engine, the program that reads
sites, keeps one overlay for the life of its process, built from the rows of `js_sites` as it starts (`reload`) and
again whenever a datagram says a site may have changed (`heard`, registered with `notice.observe`). The page reads
no site and keeps none. It is keyed on the host, never on a name.

Who enters it, and only them: a row the owner added (`added_by` owner), on the search list, switched on. A row the app
made (`app`), a site to avoid, a switched-off row and a removed row never do, and a host that a runbook covers is the
runbook's (a runbook wins; a runbook that failed to load keeps the host fail-closed, `registry.RunbookUnavailable`).
Nothing here opens the owner's one exception (`Fetcher.read_exempt`, `ROBOTS_EXEMPT_SETTING`,
`PUT /sites/robots-exempt`): an added host never goes through it.

Standard library and the schema only at import time; the database and the registry are reached inside `rebuild`."""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: The longest pace, in seconds between two requests to one host, that an added site's asks can reach: a larger
#: Crawl-delay or terms ask is held to this one number.
MAX_PACE_SECONDS = 600.0

#: What a row's owner-added mark is (`sites.where.OWNER_ADDED`; written here so this module imports nothing of it).
_OWNER_ADDED = "owner"
_SEARCH = "search"


@dataclass(frozen=True)
class Added:
    """What an added host asks of a reader: the seconds between two requests its terms ask for (0.0: none stated) and
    the hours of the day, as (start, end) pairs, it asks not to be read in (empty: none). `follow`: the site's learned
    runbook follows each role to the employer's own site (the tested original is kept on the recipe, `follow`).
    `refusals`: the statuses and page words the site's record and recipe add to the shared refusal table."""

    pace_seconds: float = 0.0
    avoid_hours: tuple[tuple[int, int], ...] = ()
    follow: bool = False
    #: A refusal table the site's record or its recipe carries (`sites/refusals.py`, `sanitize`): only ever more careful
    #: than the defaults. Empty: none.
    refusals: dict = field(default_factory=dict)


#: The overlay of this process: the host (`key_of`) to what it asks. Replaced whole by `replace`; never added to.
_hosts: dict[str, Added] = {}


def key_of(host: str | None) -> str:
    """The host as the overlay keeps it: lower case, no trailing dot, no `www.`. Empty when there is none."""
    return (host or "").strip().lower().rstrip(".").removeprefix("www.")


def owner_added_search(site: Any) -> bool:
    """Whether a site row is one the owner added to the search list (the only kind that is read as an added site)."""
    return site.added_by == _OWNER_ADDED and site.list_name == _SEARCH


def get(host: str | None) -> Added | None:
    """What this host asks, when it is an added site's host; None for every other host."""
    return _hosts.get(key_of(host))


def has(host: str | None) -> bool:
    return key_of(host) in _hosts


def pace(host: str | None) -> float:
    """The seconds between two requests this added host's terms ask for; 0.0 when none is stated or it is no added
    host. The fetcher takes the largest of this, the host's Crawl-delay and its own default (`Fetcher._every`), so
    nothing here can lower a pace."""
    found = get(host)
    return found.pace_seconds if found else 0.0


def avoids_now(host: str | None, now: Any = None) -> bool:
    """Whether this added host asked not to be read at this hour of the owner's day (its terms' `avoid_hours`, already
    held to the off hours it overlaps and to part of them: `terms_read.cap_hours`). False for any other host. A read
    that hits it waits for a later hour; nothing here stops a host or lowers a pace."""
    from app.timeutil import owner_zone, utc_now

    found = get(host)
    if not found or not found.avoid_hours:
        return False
    hour = (now or utc_now()).astimezone(owner_zone()).hour
    return any(start <= hour < end for start, end in found.avoid_hours)


def follow_domains() -> tuple[str, ...]:
    """The registrable domains of the added sites whose learned runbook follows each role to the employer's own site
    (the overlay's `follow`): what `browser/pace.follow_domains` adds to the registry's."""
    from app.jobsearch.fetch import host_stops

    return tuple(dict.fromkeys(host_stops.domain_of(h) for h, a in sorted(_hosts.items()) if a.follow))


def pace_for_domain(domain: str | None) -> float:
    """The most seconds between two page loads that any added host of this registrable domain is held to (its terms'
    ask, what was learned, a recipe's `pace_seconds`); 0.0 when none is slowed, as `registry.pace_for_domain` has it."""
    from app.jobsearch.fetch import host_stops

    if not domain:
        return 0.0
    return max((a.pace_seconds for h, a in _hosts.items() if host_stops.domain_of(h) == domain), default=0.0)


def refusals_of(host: str | None) -> dict:
    """The refusal table (`sites/refusals.py`) the site's record and recipe add for this host, or for the added hosts of
    its registrable domain when it is not one itself (a recipe's data request may go to a neighbour of the page's host);
    empty for any other host. Only ever more careful than the shared defaults."""
    from app.jobsearch.fetch import host_stops
    from app.sites import refusals

    key = key_of(host)
    if (found := _hosts.get(key)) is not None:
        return dict(found.refusals)
    out: dict = {}
    for h, a in _hosts.items():
        if a.refusals and host_stops.domain_of(h) == host_stops.domain_of(key):
            out = refusals.combine(out, a.refusals)
    return out


def learn(host: str | None, pace_seconds: float = 0.0, avoid_hours: tuple[tuple[int, int], ...] = (),
          follow: bool = False) -> None:
    """Take what was just kept for an added host into this process's overlay at once, without waiting for the rebuild:
    the pace only ever rises, the hours are added to, and a follow is switched on, never off. No effect on a host
    that is not an added one."""
    key = key_of(host)
    have = _hosts.get(key)
    if have is None:
        return
    _hosts[key] = Added(max(have.pace_seconds, _seconds(pace_seconds)),
                        tuple(dict.fromkeys((*have.avoid_hours, *avoid_hours))), have.follow or follow, have.refusals)


#: What the app's own rest of an added host (a rate-limit page, twice before a success) raises its pace to when none is
#: learned yet: the base is the slowest page gap the browser keeps (`browser/pace.PAGE_GAP`), and each rest doubles it.
REST_BASE_SECONDS = 12.0


async def slow_down(session: Any, owner: str, host: str | None) -> float:
    """The host's pace after a rate-limit rest that came again before a success: double the pace it is read at now
    (terms, learned or recipe, never less than `REST_BASE_SECONDS`), to at most `MAX_PACE_SECONDS`, kept on the rows of
    the site as `polite.learned_pace` and in the overlay at once. Never lowers: a pace already as slow is left as it
    is. Returns the pace the host is now held to; 0.0 for a host that is not an added one."""
    from sqlalchemy import select

    from app.jobsearch.fetch import host_stops
    from app.sites.models import Site

    key = key_of(host)
    have = _hosts.get(key) or next((a for h, a in _hosts.items() if host_stops.domain_of(h) == key), None)
    if have is None:
        return 0.0
    target = min(max(have.pace_seconds, REST_BASE_SECONDS) * 2, MAX_PACE_SECONDS)
    for row in (await session.scalars(select(Site).where(Site.owner == owner))).all():
        site_host = host_of(row.url)
        if row.added_by == _OWNER_ADDED and row.list_name == _SEARCH and (
                site_host == key or host_stops.domain_of(site_host) == key):
            kept = row.polite if isinstance(row.polite, dict) else {}
            row.polite = {**kept, "learned_pace": max(_seconds(kept.get("learned_pace")), target)}
            learn(site_host, target)
    await session.flush()
    return target


def hosts() -> tuple[str, ...]:
    return tuple(sorted(_hosts))


def replace(entries: dict[str, Added]) -> None:
    """Make `entries` the overlay (whole: a host not in it leaves at once). Nothing else of the process changes: a stop
    kept for a host stays as it is (`host_stops`), neither extended nor cleared."""
    global _hosts
    _hosts = dict(entries)


def clear() -> None:
    replace({})


def _seconds(value: Any) -> float:
    """A pace as a number of seconds in 0 to `MAX_PACE_SECONDS`; 0.0 for anything that is not a positive number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return 0.0
    return min(float(value), MAX_PACE_SECONDS)


def _pace_of(polite: Any) -> float:
    """The pace a row's `polite` record asks for: what its terms ask (`rate_seconds`) or what the app learned by being
    rested (`learned_pace`, `slow_down`), whichever is slower, clamped to `MAX_PACE_SECONDS`; 0.0 when it names none."""
    if not isinstance(polite, dict):
        return 0.0
    return max(_seconds(polite.get("rate_seconds")), _seconds(polite.get("learned_pace")))


def _hours_of(polite: Any) -> tuple[tuple[int, int], ...]:
    """The hours a row's `polite` record asks to avoid, as whole-hour pairs of 0..24; any other pair is left out."""
    raw = polite.get("avoid_hours") if isinstance(polite, dict) else None
    out: list[tuple[int, int]] = []
    for pair in raw if isinstance(raw, list) else ():
        if (isinstance(pair, (list, tuple)) and len(pair) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 24 for x in pair)):
            out.append((pair[0], pair[1]))
    return tuple(out)


def host_of(address: str | None) -> str:
    """The overlay key of a site's address."""
    text = (address or "").strip()
    try:
        return key_of(urlsplit(text if "//" in text else f"//{text}").hostname)
    except ValueError:
        return ""


def follow_problem(site_url: str | None, follow: Any, boards: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Why a learned runbook's `follow` (the tested original of a role, on the employer's own site) is refused, or None
    when it stands: it must be a public https address, on a host other than the site's own (or one under it), and not
    a job board's (one the app knows by name, or one of the owner's job sites in `boards`)."""
    from app.jobsearch.address import is_board_link
    from app.sites.skills import public_url

    if not isinstance(follow, str) or not public_url(follow) or urlsplit(follow).scheme != "https":
        return "follow is the address of one role on the employer's own site, a public https address"
    host, own = host_of(follow), host_of(site_url)
    if not host or host == own or host.endswith("." + own) or own.endswith("." + host):
        return "follow is on the site's own host: it must lead to the employer's own site, not stay on the board"
    if is_board_link(follow, boards):
        return "follow is a job board's address: it must lead to the employer's own site"
    return None


def _reads_apply(recipe: dict) -> bool:
    """Whether a code recipe (a data request, an html page or a page read in Chrome) says where each role's own apply
    or employer address sits (`apply`): its roles are followed to the employer's site, as a session-learned board's
    are by `follow`. The address itself is checked role by role when it is read (`skills.employer_address`)."""
    return recipe.get("kind") in ("request", "embedded", "html", "page") and bool(recipe.get("apply"))


def entries_of(rows: list[Any], found: Any, learned: dict[Any, dict] | None = None,
               boards: frozenset[str] | set[str] = frozenset()) -> dict[str, Added]:
    """The overlay the rows make. A row enters only when the owner added it, it is on the search list and switched on;
    a host a runbook covers, or one whose runbook did not load, is left to the registry `found`. Two rows of one host
    take the slower pace and both rows' hours. `learned` is each site's current recipe by site id: its `pace_seconds`
    can only raise the pace (the largest of terms, learned and recipe stands, never lower than the fetcher's own
    default), and its `follow` counts only when it stands (`follow_problem`), or when a code recipe names where the
    role's own address sits (`apply`). A refusal table of the row's record and of its recipe is added to the host's,
    whichever is more careful (`refusals.combine`)."""
    from app.sites import refusals

    out: dict[str, Added] = {}
    for row in rows:
        if row.added_by != _OWNER_ADDED or row.list_name != _SEARCH or not row.on:
            continue
        host = host_of(row.url)
        if not host or found.for_host(host) is not None or found.refusal(host) is not None:
            continue
        recipe = (learned or {}).get(getattr(row, "id", None)) or {}
        have = out.get(host, Added())
        out[host] = Added(
            max(have.pace_seconds, _pace_of(row.polite), _seconds(recipe.get("pace_seconds"))),
            tuple(dict.fromkeys((*have.avoid_hours, *_hours_of(row.polite)))),
            have.follow or (bool(recipe.get("follow"))
                            and follow_problem(row.url, recipe.get("follow"), boards) is None)
            or _reads_apply(recipe),
            refusals.combine(refusals.combine(have.refusals, (row.polite or {}).get("refusals")
                                              if isinstance(row.polite, dict) else None), recipe.get("refusals")))
    return out


async def _recipes_of(session: Any, rows: list[Any]) -> dict[Any, dict]:
    """The recipe in force of each of these sites, as saved: the live recipe of a recording, else the skill's newest
    version that is not retired. A site with neither has no entry."""
    from sqlalchemy import select

    from app.sites.models import SiteRecipe, SiteSkill

    ids = [r.id for r in rows]
    if not ids:
        return {}
    out: dict[Any, dict] = {}
    for skill in (await session.scalars(select(SiteSkill).where(
            SiteSkill.site_id.in_(ids), SiteSkill.retired_at.is_(None)).order_by(SiteSkill.version))).all():
        out[skill.site_id] = skill.recipe or {}
    for recipe in (await session.scalars(select(SiteRecipe).where(
            SiteRecipe.site_id.in_(ids), SiteRecipe.retired_at.is_(None), SiteRecipe.state == "live")
            .order_by(SiteRecipe.version))).all():
        have = out.get(recipe.site_id) or {}
        mine = recipe.recipe or {}
        out[recipe.site_id] = {**have, **{k: v for k, v in mine.items() if v is not None}}
    return out


async def rebuild(session: Any, owner: str) -> int:
    """Read the owner's sites and make the overlay of them; returns how many hosts it holds. Reads only."""
    from sqlalchemy import select

    from app.jobsearch.employers import follow as employer_follow
    from app.sites import registry
    from app.sites.models import Site

    rows = list((await session.scalars(select(Site).where(Site.owner == owner))).all())
    mine = [r for r in rows if r.added_by == _OWNER_ADDED and r.list_name == _SEARCH and r.on]
    replace(entries_of(rows, registry.current(), await _recipes_of(session, mine),
                       await employer_follow.job_board_hosts(session, owner)))
    return len(_hosts)


async def reload() -> int:
    """`rebuild` on a reader of its own, for the process's start and for a datagram. A failure leaves the overlay as it
    was and is logged: an overlay that is too small only reads an added site by the rules of every other host."""
    from app import db
    from app.settings import settings

    try:
        async with db.read_session_factory()() as session:
            return await rebuild(session, settings.owner)
    except Exception:
        log.warning("added sites: the overlay could not be rebuilt", exc_info=True)
        return len(_hosts)


#: The items of a page-bound datagram that say a site may have changed (`live.py` tells `site` for a made, changed or
#: removed row, and `resync` when it cannot say what).
_SITE_ITEMS = ("site", "resync")
_task: asyncio.Task | None = None
_again = False


def concerns(message: Any) -> bool:
    """Whether a datagram may mean a site changed: any bell (the page rang the engine after it wrote), or a page-bound
    notice that names a site or says to read everything again."""
    if getattr(message, "bell", None) is not None:
        return True
    return any(item and item[0] in _SITE_ITEMS for item in getattr(message, "items", ()) or ())


async def _settle() -> None:
    global _again
    while True:
        _again = False
        await reload()
        if not _again:
            return


def heard(message: Any) -> None:
    """The observer a program registers with `notice.observe`: a datagram that may mean a site changed makes the
    overlay again, one rebuild at a time (a datagram that comes while one runs asks for one more after it). Called on
    the running loop; it never raises and never waits."""
    global _task, _again
    if not concerns(message):
        return
    if _task is not None and not _task.done():
        _again = True
        return
    try:
        _task = asyncio.get_running_loop().create_task(_settle())
    except RuntimeError:  # no loop: nothing to rebuild on
        return


async def settled() -> None:
    """Wait for a rebuild a datagram began (the tests, and a stop)."""
    while _task is not None and not _task.done():
        await asyncio.wait([_task])
