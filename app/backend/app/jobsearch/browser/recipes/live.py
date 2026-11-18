"""A site's recipe is its reader (the owner, 2026-10-22). A site that has a live
`SiteRecipe` (made by `browser/recipes/record.py`) is read each night by that recipe, with no model and no browser, and
its
postings go into the index like any board's (`site_reads.py`). The browser no longer reads the site's listings
(`browser_reader.browser_sites`). This module is what keeps the recipe honest.

**Details.** A recipe with a `detail` request has each posting's description read by it (`with_details`), for the
postings the index holds no text of yet, at most `DETAILS_A_NIGHT` a night: every request goes through the night's
polite fetcher (robots.txt, about one request a second a host, an honest agent, a refusal is a stop).

**Health, each night.** A night that failed, read no postings, read far fewer than the recent median, or no longer
fills in the required fields is a bad night (`recipes.judge_night`); zero rows is a failure and never "nothing new".
A block is a stop and not a bad night: it never asks for a rewrite. After `recipes.STALE_AFTER` (two) bad nights in a
row the recipe is stale, and the site goes back to the browser meanwhile (a stale recipe is not live); a re-record
is asked for (`recipe_record.request`, reason `repair`): at most one rewrite of a site a week, and after
`recipes.REWRITES_MAX` failed rewrites the recipe needs the owner's attention. The rewrite, once verified, is live
at once.

**A host's posting recipe** (`SiteRecipe` with a `host` and no site, kind `posting`) reads the single-posting pages
of an employer host that has no site row, for the browser's read items on that host (`browser_code.read_by_code`): the
posting is read by code, and a page it cannot read goes to the browser as before. Its health is the reads of the day:
a day that failed some and read none is a bad day, and after the same number of bad days in a row it is stale (the
browser covers) and a re-record is asked for (`recipe_record.request_host`).

**The first read, and the chain** (a site the owner added, learned at once). A recipe just kept for an added site is
read once, now, before any night judges it (`first_read`, launched when the recipe's transaction commits,
`after_kept`): one title and one place from Search settings, the first page, no paging, at the fetcher's own polite
pace. A page that
holds postings leaves the recipe as it is. A 401 or 403, or no postings at all, means the recipe does not work on this
site, so it is retired the same day (it is no bad night, and is never counted as one) and the next stage of the chain
is asked for, cheapest first: a data request goes to the page's own HTML read by plain fetch (reason `demote`, with the
capture it was made from), the HTML to a recording in the app's Chrome (reason `chain`, once a site). Any other failure
(a rate limit, the network) is left to tonight's read. An owner with two titles also has the second read: it must
read roles and not the first title's list (`same_roles`, PRD 37.17), else the recipe is marked stale and a re-record
is asked through the stale path. A page recipe was read for real when it was recorded
(`page_run.verify`), so it needs no second look. Nothing here knows a site or a selector: the kind of recipe and what
the read gave decide.

Services flush; the night's step commits."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.browser import reader as br
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, timeup
from app.jobsearch.models import JobPosting
from app.sites import added, recipes, same_roles, search_fill, skills
from app.sites.models import Site, SiteRecipe, SiteRecording
from app.timeutil import owner_today

log = logging.getLogger(__name__)

#: How many detail requests one site's night may make (the fetcher's own pace and the night's time apply too).
DETAILS_A_NIGHT = 400


async def with_details(session: AsyncSession, fetch: Fetcher, row: SiteRecipe, listing, source_id) -> int:
    """Put on each posting of the listing that the index holds no text of the description its recipe's detail request
    gives. Returns how many details were read. A posting's page failing is no reason to stop the site's night; a block
    is."""
    recipe = skills.parse_recipe(row.recipe)
    if not isinstance(recipe, skills.RequestRecipe) or recipe.detail is None:
        return 0
    have = set((await session.scalars(select(JobPosting.ext_id).where(
        JobPosting.owner == row.owner, JobPosting.employer_id == source_id,
        func.length(func.coalesce(JobPosting.text, "")) > 0))).all())
    await db.release_if_free(session)  # up to 400 pages at the polite pace: none read inside a transaction
    read, left = 0, DETAILS_A_NIGHT
    for p in listing.postings:
        ext_id = p.get("ext_id")
        if left <= 0 or timeup(fetch):
            break
        if not ext_id or ext_id in have:
            continue
        left -= 1
        try:
            got = await skills.read_detail(fetch, recipe, ext_id)
        except Exception as exc:
            if recipes.blocked(exc):
                break
            continue
        read += 1
        if got.text:
            p["text"] = got.text
    return read


async def _ask_repair(session: AsyncSession, row: SiteRecipe, site: Site, now: datetime) -> str | None:
    """Ask for a re-record of a site whose recipe is stale: not when one is asked for already, not within a week of the
    last, and not after `recipes.REWRITES_MAX` failed rewrites (then the owner is told). Returns what was done."""
    if await recipe_record.attention(session, row.owner, site.id) >= recipes.REWRITES_MAX:
        row.state = "attention"
        return "needs attention"
    last = await session.scalar(select(func.max(SiteRecording.requested_at)).where(
        SiteRecording.site_id == site.id, SiteRecording.reason == "repair"))
    if last is not None and now - last < timedelta(days=recipes.REWRITE_EVERY_DAYS):
        return None
    await recipe_record.request(session, row.owner, site, "repair", now)
    return "re-record asked"


async def settle(session: AsyncSession, row: SiteRecipe, site: Site, night: dict, now: datetime) -> str | None:
    """Put a night in the recipe's health and act on it. `night` is {rows, stats, error, blocked, empty, details,
    requests} (`empty`: the site said there is nothing, `sites/refusals.py`). Returns a word for the night: `ok`,
    `blocked`, `bad`, or what a repair did (the recipe is then no longer live)."""
    history = list(row.health or [])
    entry = {"at": now.isoformat(), "rows": night["rows"], "requests": night.get("requests", 0),
             "details": night.get("details", 0), "fill": night["stats"]}
    empty = bool(night.get("empty")) and not night["error"] and not night["rows"]
    if night["blocked"]:
        entry["why"] = "blocked"
        word: str | None = "blocked"
    elif why := recipes.judge_night(history, night["rows"], night["stats"], night["error"], empty):
        entry["why"], word = why, "bad"
        row.bad_nights = (row.bad_nights or 0) + 1
    elif empty:
        entry["why"], word = "empty", "ok"  # the site said there is nothing: no bad night
    else:
        row.bad_nights, word = 0, "ok"
    row.health = [*history, entry][-recipes.HISTORY:]
    if word == "bad" and row.bad_nights >= recipes.STALE_AFTER and row.state == "live":
        row.state = "stale"
        word = await _ask_repair(session, row, site, now) or "stale"
    return word


async def posting_recipe_for(session: AsyncSession, owner: str, link: str | None) -> SiteRecipe | None:
    """The live posting recipe of the host this posting address is on (the narrowest host that covers it), or None."""
    host = br.host_of(link)
    if not host:
        return None
    rows = [r for r in (await session.scalars(select(SiteRecipe).where(
        SiteRecipe.owner == owner, SiteRecipe.site_id.is_(None), SiteRecipe.host.is_not(None),
        SiteRecipe.retired_at.is_(None), SiteRecipe.state == "live"))).all()
        if r.host and skills.host_covers(r.host, host)]
    return min(rows, key=lambda r: (r.host or "").startswith("*."), default=None)


async def page_recipe_for(session: AsyncSession, owner: str, link: str | None) -> SiteRecipe | None:
    """The live page or HTML recipe of the site this posting address is on (the same site: the last two labels of the
    host match), or None. Its text element reads the posting's page by code (`page_recipe.read_posting` in the app's
    Chrome, `html_recipe.read_text` by plain fetch)."""
    host = br.host_of(link)
    if not host:
        return None

    def site_of(h: str) -> str:
        return ".".join(h.split(".")[-2:])

    rows = [r for r in (await session.scalars(select(SiteRecipe).where(
        SiteRecipe.owner == owner, SiteRecipe.site_id.is_not(None), SiteRecipe.retired_at.is_(None),
        SiteRecipe.state == "live").order_by(SiteRecipe.created_at.desc()))).all()
        if (r.recipe or {}).get("kind") in ("page", "html") and (r.recipe or {}).get("text")
        and site_of(br.host_of(r.recipe.get("url")) or "") == site_of(host)]
    return rows[0] if rows else None


async def _ask_repair_host(session: AsyncSession, row: SiteRecipe, now: datetime) -> str | None:
    """`_ask_repair` for a host's posting recipe."""
    assert row.host
    if await recipe_record.attention_host(session, row.owner, row.host) >= recipes.REWRITES_MAX:
        row.state = "attention"
        return "needs attention"
    last = await session.scalar(select(func.max(SiteRecording.requested_at)).where(
        SiteRecording.host == row.host, SiteRecording.reason == "repair"))
    if last is not None and now - last < timedelta(days=recipes.REWRITE_EVERY_DAYS):
        return None
    return "re-record asked" if await recipe_record.request_host(session, row.owner, row.host, "repair", now) else None


def _close_days(row: SiteRecipe, today: str) -> None:
    """Judge the days of a posting recipe's reads that are over: a day that failed some and read none is bad; two bad
    days in a row make the recipe stale. A day is judged once (`judged`)."""
    health = [dict(h) for h in (row.health or [])]
    for entry in health:
        if entry.get("day", today) < today and not entry.get("judged"):
            entry["judged"] = True
            bad = bool(entry.get("failed")) and not entry.get("rows")
            row.bad_nights = (row.bad_nights or 0) + 1 if bad else 0
            if bad:
                entry["why"] = "it read none of the postings it was asked for"
    row.health = health[-recipes.HISTORY:]
    if (row.bad_nights or 0) >= recipes.STALE_AFTER and row.state == "live":
        row.state = "stale"


async def note_posting_read(session: AsyncSession, row_id, ok: bool, now: datetime | None = None) -> None:
    """A read of one posting by a host's posting recipe went well (`ok`) or did not: counted in the day's entry of its
    health. A day over is judged first (`_close_days`); a recipe that went stale asks for a re-record."""
    now = now or datetime.now(UTC)
    row = await session.get(SiteRecipe, row_id)
    if row is None:
        return
    today = owner_today(now).isoformat()
    _close_days(row, today)
    health = [dict(h) for h in (row.health or [])]
    if not health or health[-1].get("day") != today:
        health.append({"day": today, "at": now.isoformat(), "rows": 0, "failed": 0})
    health[-1]["rows" if ok else "failed"] = int(health[-1].get("rows" if ok else "failed", 0)) + 1
    row.health = health[-recipes.HISTORY:]
    if row.state == "stale":
        await _ask_repair_host(session, row, now)
    await session.flush()


async def maintain(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """The stale recipes, each night: a week after the last, a new re-record is asked for (the site is read by the
    browser meanwhile). A host's posting recipe has the days of its reads judged first. Returns how many were asked."""
    now = now or datetime.now(UTC)
    asked = 0
    for row in (await session.scalars(select(SiteRecipe).where(
            SiteRecipe.owner == owner, SiteRecipe.site_id.is_(None), SiteRecipe.host.is_not(None),
            SiteRecipe.retired_at.is_(None), SiteRecipe.state.in_(("live", "stale")))
            .order_by(SiteRecipe.created_at))).all():
        _close_days(row, owner_today(now).isoformat())
        if row.state == "stale" and await _ask_repair_host(session, row, now) == "re-record asked":
            asked += 1
    for row in (await session.scalars(select(SiteRecipe).where(
            SiteRecipe.owner == owner, SiteRecipe.site_id.is_not(None), SiteRecipe.retired_at.is_(None),
            SiteRecipe.state == "stale").order_by(SiteRecipe.created_at))).all():
        site = await session.get(Site, row.site_id)
        if site is not None and site.on and await _ask_repair(session, row, site, now) == "re-record asked":
            asked += 1
    await session.flush()
    return asked


# --- the first read of a recipe kept for an added site, and the chain -------------------------------------------------

#: The recipe kinds the first read looks at (a page recipe was read for real when it was recorded).
FIRST_READ_KINDS = ("request", "html")
#: What the two links of the chain ask for are named (`recipe_record.OWNER_ASKED`).
DEMOTE, CHAIN = "demote", "chain"
#: The statuses of a refusal that says "not this way": the recipe goes to the next stage. Any other failure is
#: tonight's read's to try again.
NOT_THIS_WAY = (401, 403)
_tasks: set[asyncio.Task] = set()


async def _first_read_safely(owner: str, site_id) -> None:
    try:
        await first_read(owner, site_id)
    except Exception:  # the first read is a courtesy to the night's: whatever it does, nothing else is lost
        log.warning("the first read of the site %s failed", site_id, exc_info=True)


def _launch(owner: str, site_id) -> None:
    """Run `first_read` as a task of the engine's loop (kept until it ends)."""
    task = asyncio.get_running_loop().create_task(_first_read_safely(owner, site_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


#: What `after_kept` calls once the keeping transaction has committed (tests replace it).
LAUNCH = _launch


async def after_kept(session: AsyncSession, owner: str, site_id) -> None:
    """A recipe was kept for a site: when the owner added the site, its first read is launched once the transaction
    that keeps it has committed (`first_read`). Any other site waits for the night."""
    if site_id is None:
        return
    site = await session.get(Site, site_id)
    if site is not None and added.owner_added_search(site):
        db.after_commit(session, lambda: LAUNCH(owner, site_id))


async def _demote(session: AsyncSession, owner: str, site: Site, row: SiteRecipe, why: str,
                  now: datetime) -> str | None:
    """Take a recipe that read nothing out of force, with no bad night, and ask for the chain's next stage: a data
    request goes to the page's HTML (`demote`, with its capture), the HTML to the app's Chrome (`chain`, once a site).
    Returns the stage asked for, or None when there is none (the site is read in the browser, as before)."""
    row.state, row.retired_at = "retired", now
    kind = (row.recipe or {}).get("kind")
    if kind == "request":
        if await session.scalar(select(SiteRecording.id).where(
                SiteRecording.site_id == site.id, SiteRecording.reason == DEMOTE).limit(1)) is not None:
            return None
        made = await session.get(SiteRecording, row.recording_id) if row.recording_id is not None else None
        session.add(SiteRecording(owner=owner, site_id=site.id, reason=DEMOTE, state="requested", requested_at=now,
                                  url=site.url, body=made.body if made is not None and made.body else recipes.pack({}),
                                  note=f"the first read of the data recipe {why}"))
        return "html"
    if kind == "html":
        if await session.scalar(select(SiteRecording.id).where(
                SiteRecording.site_id == site.id, SiteRecording.reason == CHAIN).limit(1)) is not None:
            return None
        await recipe_record.request(session, owner, site, CHAIN, now)
        return "page"
    return None


async def _suspect(owner: str, site_id, row_id) -> str:
    """The recipe read, but a second title read nothing or the first title's list: it is marked stale and a re-record
    is asked for through the stale path (`_ask_repair`, the week's limit and the three tries apply). Returns
    `suspect`."""
    async with db.session_factory()() as session:
        row = await session.get(SiteRecipe, row_id)
        site = await session.get(Site, site_id)
        if row is None or site is None or row.retired_at is not None or row.state != "live":
            return "skipped"
        row.state = "stale"
        await _ask_repair(session, row, site, datetime.now(UTC))
        await session.commit()
    return "suspect"


async def first_read(owner: str, site_id) -> str:
    """Read the site once with the recipe now in force for it: one title and one place, the first page, no paging.
    Returns `ok` (it read postings, and, for an owner with two titles, the second title's too, not the first's list:
    else `suspect`, the recipe is stale and a re-record is asked), `skipped` (nothing to look at), `error` (a failure
    that is the night's to try again) or `html` / `page` / `none` (the recipe read nothing, was retired, and the
    stage asked for, if any)."""
    from app.jobsearch.browser.recipes import html_run as html_recipe  # it uses this module's neighbours

    async with db.session_factory()() as session:
        site = await session.get(Site, site_id)
        row = await session.scalar(select(SiteRecipe).where(
            SiteRecipe.site_id == site_id, SiteRecipe.retired_at.is_(None), SiteRecipe.state == "live")
            .order_by(SiteRecipe.version.desc()).limit(1))
        if (site is None or row is None or not added.owner_added_search(site) or not site.on
                or (row.recipe or {}).get("kind") not in FIRST_READ_KINDS):
            return "skipped"
        found = await search_fill.read(session, owner)
        recipe, row_id = dict(row.recipe), row.id
    parsed = skills.parse_recipe(recipe)
    uses_title = bool(parsed.blanks()) if isinstance(parsed, skills.RequestRecipe) else bool(
        isinstance(parsed, skills.HtmlRecipe) and parsed.template())
    if uses_title and not found.phrases:
        return "skipped"  # it searches the owner's titles, and there are none yet
    # A board whose requests carry its own value for each place is read with a place it has a value for.
    mapped = getattr(parsed, "place_map", None)
    place = [p for p in found.places if search_fill.place_key(p) in mapped][:1] if mapped else found.places[:1]
    one = search_fill.Search(found.phrases[:1], tuple(place))
    fetch = polite.make()
    verdict, why = "ok", ""
    suspect = False

    async def read(search: search_fill.Search) -> list[dict]:
        if isinstance(parsed, skills.HtmlRecipe):
            return (await html_recipe.run(fetch, parsed, enough=1, pages=1, search=search)).postings
        return (await skills.run_recipe(fetch, parsed, enough=1, search=search)).postings

    try:
        first = await read(one)
        if not first:
            verdict, why = "empty", "read no postings"
        elif uses_title and len(found.phrases) >= 2:
            # The owner's second title must read roles too, and not the first title's list: a search that ignores
            # the words returns one list whatever it is asked (PRD 37.17).
            second = await read(search_fill.Search(found.phrases[1:2], tuple(place)))
            suspect = not second or same_roles.same_list(first, second)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status in NOT_THIS_WAY or getattr(fetch, "refused_hosts", None):  # this read was answered 401 or 403
            verdict, why = "refused", f"was refused ({status or 403})"
        else:
            verdict = "error"
            log.info("the first read of %s failed: %s", site_id, exc)
    finally:
        await fetch.close()
    if suspect:
        return await _suspect(owner, site_id, row_id)
    if verdict == "ok":
        return "ok"
    if verdict == "error":
        return "error"
    async with db.session_factory()() as session:
        row = await session.get(SiteRecipe, row_id)
        site = await session.get(Site, site_id)
        if row is None or site is None or row.retired_at is not None:
            return "skipped"
        stage = await _demote(session, owner, site, row, why, datetime.now(UTC))
        await session.commit()
    return stage or "none"
