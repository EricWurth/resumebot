"""One line that says what the app learned of how to read a site the owner added (docs/PRD.md 37.17, item G; the
owner, 2026-11-17: "yes to all five. go."): the way it is read, which of the title, the place and the date its search
fills, its pace and where that came from, and, when a stage of learning failed, which stage and why. It is added to
the site's status in Where to look: no screen, panel or control.

Made by code from the stored recipe, the site's `polite` record and its recordings; no model writes a word of it, and it
reads and changes nothing else. Only a site the owner added has one (a runbook, a job system's reader or a site the app
made is not learned). Standard library and the schema only; no site is named."""

import json
import re
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites import added, search_fill
from app.sites.models import Site, SiteRecipe, SiteRecording
from app.sites.schema import DEFAULT_PACE_SECONDS
from app.textutil import one_line

_BLANK = re.compile(r"\{(phrase|place|since)\}")
#: How each learned recipe kind is read, as the line says it.
HOW = {
    "request": "a data request",
    "embedded": "the data the page carries",
    "list_page": "the page's links",
    "html": "small HTML from the page",
    "page": "the app's Chrome",
    "browser": "a browser session",
}
#: The stage a failed recording was for, by what it was asked for (`browser/recipes/record.OWNER_ASKED`).
STAGES = {"first": "first recording", "page": "page recording", "chain": "app's Chrome", "demote": "page's HTML"}
#: What a first read says when it demotes a recipe, before the reason (`browser/recipes/live._demote`).
_DEMOTE_PREFIX = "the first read of the data recipe "
WHY_MAX = 120


class Failure:
    """The stage that failed and why: a name from `STAGES` or `data request`, and a short reason."""

    def __init__(self, stage: str, why: str) -> None:
        self.stage, self.why = stage, why


def _blanks(recipe: dict) -> set[str]:
    """The search blanks a code recipe fills on every read: the title, the place and the date window."""
    kind = recipe.get("kind")
    if kind in ("request", "embedded"):
        text = json.dumps([recipe.get("url"), recipe.get("params"), recipe.get("body")], default=str)
    elif kind == "html":
        text = f"{recipe.get('search_url') or ''} {recipe.get('fragment_url') or ''}"
    elif kind == "page":
        return ({"phrase"} if recipe.get("search") else set()) | ({"place"} if recipe.get("place") else set())
    elif kind == "browser":
        search = recipe.get("search") or {}
        template = search.get("url_template") or ""
        found = set(_BLANK.findall(template))
        if search.get("how") == "boxes":
            found |= {"phrase"} if search.get("phrase_box") else set()
            found |= {"place"} if search.get("place_box") else set()
        if search.get("recency"):
            found.add("since")
        return found
    else:
        return set()
    return set(_BLANK.findall(text))


def _fills(recipe: dict) -> str:
    blanks = _blanks(recipe)
    names = [word for key, word in (("phrase", "title"), ("place", "place"), ("since", "date")) if key in blanks]
    if not names:
        return "fills none of your search words"
    out = f"fills {names[0]}" if len(names) == 1 else f"fills {', '.join(names[:-1])} and {names[-1]}"
    if "place" not in names:
        return f"{out}; place unsupported"
    if place_map := search_fill.clean_place_map(recipe.get("place_map")):  # the board's own values: which places
        return f"{out}; places searched: {', '.join(place_map)}"
    if recipe.get("place_pick"):
        return f"{out}; places picked from the site's own list"
    return out


def _seconds(value: Any) -> float:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0.0)


def pace_words(recipe: dict | None, polite: Any) -> str:
    """The pace and where it came from: what its terms ask, what was learned (the recipe's own or from being rested), or
    the app's default, whichever is slowest. A robots.txt Crawl-delay is kept nowhere but in the engine's memory, so the
    line does not say it."""
    polite = polite if isinstance(polite, dict) else {}
    terms = _seconds(polite.get("rate_seconds"))
    learned = max(_seconds(polite.get("learned_pace")), _seconds((recipe or {}).get("pace_seconds")))
    seconds, source = max((DEFAULT_PACE_SECONDS, "default"), (learned, "learned"), (terms, "terms"))[:2]
    if seconds <= DEFAULT_PACE_SECONDS:
        return "paced at about one request a second (default)"
    return f"paced at one request every {min(seconds, added.MAX_PACE_SECONDS):g} seconds ({source})"


def line(recipe: dict | None, polite: Any = None, failure: Failure | None = None) -> str | None:
    """The line for one site, or None when there is nothing learned to say (no recipe of a learned kind and no failed
    stage). `recipe` is the recipe in force as stored, `polite` the site's record, `failure` the stage that failed."""
    kind = (recipe or {}).get("kind")
    parts: list[str] = []
    if recipe and kind in HOW:
        parts.append(f"Read by {HOW[kind]}")
        parts.append(_fills(recipe))
        parts.append(pace_words(recipe, polite))
    elif failure is None:
        return None
    if failure is not None:
        parts.append(f"the {failure.stage} stage failed: {failure.why}" if failure.why
                     else f"the {failure.stage} stage failed")
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def _failure(recordings: Iterable[SiteRecording]) -> Failure | None:
    """The latest stage that failed, from a site's recordings oldest first: a failed one is its own stage; a
    `demote` one says the data request's first read failed (its note says why). A fresh learn that was not failed
    (reason `first` or `page`) clears what came before it."""
    out: Failure | None = None
    for rec in recordings:
        why = one_line(rec.note or "", WHY_MAX).rstrip(".")
        if rec.state == "failed":
            out = Failure(STAGES.get(rec.reason, "learning"), why)
        elif rec.reason == "demote":
            out = Failure("data request", why.removeprefix(_DEMOTE_PREFIX))
        elif rec.reason in ("first", "page"):
            out = None
    return out


async def lines(
    session: AsyncSession, owner: str, sites: list[Site], skills: dict[uuid.UUID, dict],
    recordings: dict[uuid.UUID, list[SiteRecording]],
) -> dict[uuid.UUID, str]:
    """The line of each of these sites that has one, by site id. `skills` is each site's skill recipe in force and
    `recordings` each added site's recordings, oldest first (`where_view._recordings`). Reads only; a site the owner did
    not add, or that is off, has none."""
    mine = [s.id for s in sites if added.owner_added_search(s) and s.on]
    if not mine:
        return {}
    live: dict[uuid.UUID, dict] = {}
    # A recipe is always made from a recording (`recording_id`; a recording is never deleted), so a
    # site with none has no recipe of its own to look for: the page's reading of many sites stays as short as it was.
    taught = [sid for sid in mine if recordings.get(sid)]
    for row in (await session.scalars(select(SiteRecipe).where(
            SiteRecipe.owner == owner, SiteRecipe.site_id.in_(taught), SiteRecipe.retired_at.is_(None),
            SiteRecipe.state == "live").order_by(SiteRecipe.version))).all() if taught else ():
        if row.site_id is not None:
            live[row.site_id] = row.recipe or {}
    out: dict[uuid.UUID, str] = {}
    for site in sites:
        if site.id not in mine:
            continue
        said = line(live.get(site.id) or skills.get(site.id), site.polite, _failure(recordings.get(site.id, ())))
        if said:
            out[site.id] = said
    return out
