"""The search words a request recipe is filled with, and how code finds where they go. Pure: no network, no database.

**The search.** `Search` is what Search settings hold that a site's search can be typed with: the owner's titles
(`phrases`, one search each) and the places (`places`, which already hold "Remote" when remote work counts). It is read
once from the profile (`of_profile`, `read`) and handed to the recipe's run; nothing of it is stored in a recipe, so a
changed title is used the next night.

**The blanks.** A request recipe writes `{phrase}` and `{place}` where the search words go in the request its listing
is read with (`skills.RequestRecipe`). Code, never a model, says where those are: a recording of a site types the
owner's first title, then, when there is one, the second title, and then a place, and answers `typed` (what was
typed) and, on each data request, `after` (which typing it followed). `blank_places` compares the requests those
typings made: the field that held the first title and then held the second is where `{phrase}` goes; the field that
held the place when the title alone had not is where `{place}` goes. With one title only, the field must hold
exactly the typed words. Nothing is guessed: a site where the diff shows no such field has no template.

**The date window.** A third blank, `{since}`, is the board's own recency filter, filled from when the board was last
read well (`Search.since`, less `SINCE_MARGIN`, so a missed night is not lost; no setting). A recording changes the
filter of the site's own search form once (`interact`), and `blank_places` finds the field of the request that
moved with it.
What is learned of the filter's values is data (`clean_since`): a fixed list of choices (each with the seconds it
covers, read from its label), a number of seconds or of days, or a date. `since_value` fills it: the smallest choice
that covers the window, the widest on a first read; a number or date has no first-read value, and the entry is left
out. A board with no such filter has no `{since}` and is read as it is.

**Nothing typed stays.** `literals_left` finds the typed words that remain whole in a recipe's address, params or
body, whatever their length (case ignored, the address decoded): such a recipe is never kept, so a person's titles
are not stored in a recipe."""

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit

#: The three blanks a request recipe may hold: the owner's title, the owner's place, and the date window.
BLANKS = ("phrase", "place", "since")
#: The most places a recording picks from a board's own list of suggestions (the first is `place`, the rest are
#: `place2`, `place3`, ...): code finds the field that changes with the place by comparing the requests they made.
PLACES_MAX = 4
#: Which typing a recorded data request followed: the first title, the second title, each place, the recency filter.
AFTER = ("phrase", "second", "place", *(f"place{n}" for n in range(2, PLACES_MAX + 1)), "since")
#: The typings that name a place, the last one first (the one that stands in the form when the recency filter is
#: changed).
PLACE_TAGS_LAST_FIRST = tuple(reversed(AFTER[2:-1]))
#: The most places a recipe's place-to-value map keeps, and the longest value it keeps for one.
PLACE_MAP_MAX = 60
PLACE_VALUE_MAX = 200
#: How the date window's value is written: one of a fixed list of choices, seconds, days, or a date (`YYYY-MM-DD`).
SINCE_FORMATS = ("choices", "seconds", "days", "date")
#: The most choices a recency filter's list keeps.
SINCE_CHOICES_MAX = 20
#: Added to the time since the last good read, so a night that was missed is not lost.
SINCE_MARGIN = timedelta(hours=24)
#: The longest word typed that is kept.
LITERAL_MAX = 80
_BLANK = re.compile(r"\{(phrase|place|since)\}")
_ANY_BRACES = re.compile(r"\{[A-Za-z_]+\}")
#: A field of a request: ("q", a query parameter's name) or ("b", a body's dotted path).
Place = tuple[str, str]


@dataclass(frozen=True)
class Search:
    """What a night's read of a site types in its search: each phrase is one search, each place is one more (none: the
    search names no place)."""

    phrases: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    #: When the board was last read well (`last_good_read`); None for a first read, which asks for the widest window.
    since: datetime | None = None
    #: The clock (None: the real one); only a test sets it.
    now: datetime | None = None
    #: A recording's replay fills `{since}` with exactly this text.
    since_literal: str | None = None


def places_of(profile: Any) -> list[str]:
    """The places to type into a site's location box: the locations the owner would work from (hybrid or in person),
    and Remote when remote work counts, or there is no work location gate, and no place says it."""
    loc = profile.gates.location
    places = list(dict.fromkeys([*loc.hybrid.locations, *loc.in_person.locations]))
    if (loc.remote.on or not profile.gates.checks_location()) and not any("remote" in p.lower() for p in places):
        places.append("Remote")
    return places


def of_profile(profile: Any) -> Search:
    """The search the owner's profile asks for."""
    return Search(tuple(profile.criteria.search_phrases()), tuple(places_of(profile)))


async def read(session: Any, owner: str) -> Search:
    """The owner's search, read from the profile in force."""
    from app.jobsearch.profile import store as profile_store  # the profile reads the whole job-search side

    return of_profile(await profile_store.current(session, owner))


# --- what was typed ---------------------------------------------------------------------------------------------------


def _word(value: Any) -> str | None:
    """One typed word as it is kept: one line, no control characters, at most `LITERAL_MAX` long; None if empty."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch for ch in value if ch.isprintable()).split())
    return text[:LITERAL_MAX] or None


def clean_typed(raw: Any) -> dict | None:
    """What a recording says the visitor typed, as `{phrase, second, place}` (the last two may be None), or None when no
    title was typed (nothing is then a blank)."""
    if not isinstance(raw, dict):
        return None
    phrase = _word(raw.get("phrase"))
    if phrase is None:
        return None
    out: dict = {"phrase": phrase, "second": _word(raw.get("second")), "place": _word(raw.get("place"))}
    more = [w for w in dict.fromkeys(_word(m) for m in raw.get("more_places") or [] if isinstance(m, str)) if w]
    if out["place"] is not None and more:  # the places after the first that were picked from the site's own list
        out["more_places"] = [w for w in more if not _same(w, out["place"])][:PLACES_MAX - 1]
    if (since := clean_since(raw.get("since"))) is not None:
        out["since"] = since
    return out


def clean_since(raw: Any) -> dict | None:
    """What a recording learned of the site's recency filter, as `{format, before, after[, choices]}`, or None when it
    is not usable. `before` and `after` are the control's value before and after the recording changed it; `choices`
    (the `choices` format) are `{value, seconds}`, `seconds` None for "any time", and `after` is one of them with a
    window. A date or a number has no list."""
    if not isinstance(raw, dict) or raw.get("format") not in SINCE_FORMATS:
        return None
    before, after = raw.get("before") or "", raw.get("after")
    if not isinstance(before, str) or not isinstance(after, str) or not after.strip():
        return None
    if max(len(before), len(after)) > LITERAL_MAX:
        return None
    out: dict = {"format": raw["format"], "before": before, "after": after}
    if raw["format"] != "choices":
        return out
    choices: list[dict] = []
    for c in raw.get("choices") or []:
        if not isinstance(c, dict) or not isinstance(c.get("value"), str) or len(c["value"]) > LITERAL_MAX:
            continue
        seconds = c.get("seconds")
        if seconds is not None and (not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1):
            continue
        if all(c["value"] != k["value"] for k in choices):
            choices.append({"value": c["value"], "seconds": seconds})
    chosen = next((k for k in choices if k["value"] == after), None)
    if len(choices) < 2 or chosen is None or chosen["seconds"] is None:
        return None
    out["choices"] = choices[:SINCE_CHOICES_MAX]
    return out


def since_spec(typed: dict | None) -> dict | None:
    """The recipe's `since` (its format and, for a list, the choices) from what the recording learned, or None."""
    got = (typed or {}).get("since")
    if not isinstance(got, dict):
        return None
    return {"format": got["format"], **({"choices": got["choices"]} if got.get("choices") else {})}


# --- the recency filter's words ---------------------------------------------------------------------------------------

_ANY = re.compile(r"^\W*(any|anytime|any ?time|any date|all|all time|all dates|all jobs|no limit|ever)\W*$",
                  re.I)
_SPAN = re.compile(r"\b(\d+|an?|one|two|three)?\s*(hour|hr|day|week|wk|month|year)s?\b", re.I)
_UNIT = {"hour": 3600, "hr": 3600, "day": 86400, "week": 604800, "wk": 604800, "month": 2_592_000, "year": 31_536_000}
_COUNT = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


def parse_window(label: str | None) -> tuple[bool, int | None]:
    """What a recency choice's label says: (recognised, the seconds it covers); None seconds is "any time". "Past 24
    hours" is 86400, "last 7 days" 604800, "this week" 604800, "today" 86400, "any time" is no limit."""
    text = " ".join((label or "").split())
    if _ANY.match(text):
        return True, None
    if re.search(r"\btoday\b", text, re.I):
        return True, 86400
    m = _SPAN.search(text)
    if m is None:
        return False, None
    raw = (m.group(1) or "1").lower()
    count = int(raw) if raw.isdigit() else _COUNT.get(raw, 1)
    return (True, count * _UNIT[m.group(2).lower()]) if count > 0 else (False, None)


def choices_of(options: Any) -> list[dict]:
    """The usable choices of a list of `{value, label}` options: those whose label says a window (or "any time"), each
    once, as `{value, seconds}`."""
    out: list[dict] = []
    for o in options if isinstance(options, list) else []:
        if not isinstance(o, dict) or not isinstance(o.get("value"), str):
            continue
        ok, seconds = parse_window(o.get("label") if isinstance(o.get("label"), str) else None)
        if ok and all(c["value"] != o["value"] for c in out):
            out.append({"value": o["value"], "seconds": seconds})
    return out[:SINCE_CHOICES_MAX]


def last_good_read(health: Any) -> datetime | None:
    """When a board was last read well, from a recipe's nightly health (`recipes.settle`): the newest night that was
    neither bad nor blocked (a night the site said there was nothing is good). None when there is none."""
    best: datetime | None = None
    for night in health if isinstance(health, list) else []:
        if not isinstance(night, dict) or night.get("why") not in (None, "empty"):
            continue
        try:
            at = datetime.fromisoformat(str(night.get("at")))
        except ValueError:
            continue
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        best = at if best is None or at > best else best
    return best


def window_seconds(search: "Search | None") -> int | None:
    """How far back the search must reach: the time since the last good read plus `SINCE_MARGIN`, in whole seconds
    (at least one); None for a first read."""
    if search is None or search.since is None:
        return None
    now = search.now or datetime.now(UTC)
    since = search.since if search.since.tzinfo else search.since.replace(tzinfo=UTC)
    return max(1, math.ceil((now - (since - SINCE_MARGIN)).total_seconds()))


def since_value(spec: Any, search: "Search | None") -> str | None:
    """What `{since}` is filled with, as text, or None when the entry is left out. `spec` is a recipe's `since` (its
    `format` and `choices`). A choice list: the smallest choice that covers the window, the widest on a first read or
    when none covers it (an "any time" choice is the widest of all; its value may be empty, which leaves the entry
    out). Seconds, days and a date have no value for a first read (no limit)."""
    if search is not None and search.since_literal is not None:
        return search.since_literal
    window = window_seconds(search)
    if spec.format == "choices":
        choices = list(spec.choices)
        widest = next((c for c in choices if c.seconds is None), None) or max(
            (c for c in choices if c.seconds is not None), key=lambda c: c.seconds or 0, default=None)
        if window is None:
            return widest.value if widest is not None else None
        covering = [c for c in choices if c.seconds is not None and c.seconds >= window]
        pick = min(covering, key=lambda c: c.seconds or 0) if covering else widest
        return pick.value if pick is not None else None
    if window is None:
        return None
    if spec.format == "seconds":
        return str(window)
    if spec.format == "days":
        return str(math.ceil(window / 86400))
    now = (search.now if search is not None else None) or datetime.now(UTC)
    return (now - timedelta(seconds=window)).date().isoformat()


def leaf_is_number(body: Any, path: str) -> bool:
    """Whether the JSON body's field at this dotted path is a number (not text): a recipe then fills it as one."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return False
    for step in path.split(".") if path else []:
        if isinstance(body, dict) and step in body:
            body = body[step]
        elif isinstance(body, list) and step.isdigit() and int(step) < len(body):
            body = body[int(step)]
        else:
            return False
    return isinstance(body, int | float) and not isinstance(body, bool)


def literals_of(typed: dict | None) -> list[str]:
    """Every word a recording typed, once each."""
    typed = typed or {}
    words = [typed.get(k) for k in ("phrase", "second", "place")] + list(typed.get("more_places") or [])
    return list(dict.fromkeys(w for w in words if w))


# --- the fields of a request ------------------------------------------------------------------------------------------


def _walk(value: Any, path: str, out: dict[Place, str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _walk(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk(v, f"{path}.{i}" if path else str(i), out)
    elif value is not None and not isinstance(value, bool):
        out[("b", path)] = str(value)


def leaves(url: str, body: Any = None, params: dict | None = None) -> dict[Place, str]:
    """The fields of a request that a search word can sit in: each query parameter of the address (decoded) and each
    `params` entry, and each value of a JSON body (a body that is not JSON is one field, its whole text)."""
    out: dict[Place, str] = {}
    for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        out[("q", k)] = v
    for k, v in (params or {}).items():
        out[("q", k)] = str(v)
    if isinstance(body, str) and body:
        try:
            body = json.loads(body)
        except ValueError:
            out[("b", "")] = body
            return out
    if body is not None:
        _walk(body, "", out)
    return out


def _has(value: str, word: str) -> bool:
    return word.casefold() in value.casefold()


def _same(value: str, word: str) -> bool:
    return " ".join(value.split()).casefold() == " ".join(word.split()).casefold()


def blank_places(first: dict[Place, str] | None, second: dict[Place, str] | None, with_place: dict[Place, str] | None,
                 typed: dict, *, last: dict[Place, str] | None = None,
                 with_since: dict[Place, str] | None = None) -> dict[str, set[Place]]:
    """Where `{phrase}` and `{place}` may go in a request, from the fields of the request the first title made
    (`first`), the one the second title made (`second`) and the one the place made (`with_place`); None for a typing
    that made no such request. `{phrase}`: the fields that held the first title and, in the second title's request, the
    second one; with no second title, the fields that hold exactly the first title. `{place}`: the fields that hold the
    place and did not hold it before. `{since}`: the fields of the request the recency filter's change made
    (`with_since`) that hold the value the filter was changed to and, in the request of the last typing before it
    (`last`), held the value it had (or were not there): the filter's own field and nothing else that moved."""
    first_leaves = first or {}
    where: dict[str, set[Place]] = {"phrase": set(), "place": set()}
    a, b, p = typed["phrase"], typed.get("second"), typed.get("place")
    if first is not None:
        if b is None:
            where["phrase"] = {k for k, v in first_leaves.items() if _same(v, a)}
        elif second is not None:
            where["phrase"] = {k for k, v in first_leaves.items()
                               if _has(v, a) and k in second and _has(second[k], b) and not _same(v, second[k])}
    if p is not None and with_place is not None:
        where["place"] = {k for k, v in with_place.items() if _has(v, p) and not _has(first_leaves.get(k, ""), p)}
    since = typed.get("since")
    if since:  # a recording that changed the recency filter says where it went (an empty set: nowhere)
        where["since"] = set()
    if since and with_since is not None and last is not None:
        where["since"] = {k for k, v in with_since.items()
                          if _same(v, since["after"]) and not _same(last.get(k, ""), since["after"])
                          and (k not in last or _same(last[k], since["before"]))}
    return where


# --- places the site's own suggestions name -------------------------------------------------------------------------


def place_tag(n: int) -> str:
    """The typing a request followed when it was the n-th place picked (0 is the first)."""
    return "place" if n == 0 else f"place{n + 1}"


def place_key(word: str) -> str:
    """A place as the map keys it: one line, lower case."""
    return " ".join(word.split()).casefold()


def pick_suggestion(options: list[str], word: str) -> int | None:
    """Which of the visible suggestions to pick for the typed place, or None: the first whose text is exactly the place
    (case and spacing ignored), else the first that starts with it. Never any other; no match, no pick."""
    want = place_key(word)
    if not want:
        return None
    keys = [place_key(o) for o in options]
    for i, key in enumerate(keys):
        if key == want:
            return i
    for i, key in enumerate(keys):
        if key.startswith(want):
            return i
    return None


def clean_place_map(raw: Any) -> dict[str, str | int] | None:
    """A recipe's place-to-value map as it is kept: each key a place as `place_key` has it, each value the text (or
    whole number) the board's request carries for it; at most `PLACE_MAP_MAX`. None when nothing usable is left."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, str | int] = {}
    for k, v in raw.items():
        key = place_key(k) if isinstance(k, str) else ""
        if not key or len(key) > LITERAL_MAX or len(out) >= PLACE_MAP_MAX:
            continue
        if isinstance(v, bool) or not isinstance(v, str | int):
            continue
        if isinstance(v, str) and (not v.strip() or len(v) > PLACE_VALUE_MAX):
            continue
        out[key] = v
    return out or None


def mapped_places(places: Any, mapping: dict | None) -> tuple[list[tuple[str, str | int]], list[str]]:
    """The places of a read that the map has a value for, with the value, and the places it has none for (in order, each
    once). A recipe with no map is read with the place's own words, as it always was."""
    found: list[tuple[str, str | int]] = []
    left: list[str] = []
    for place in places:
        value = (mapping or {}).get(place_key(place))
        if value is None:
            if place not in left:
                left.append(place)
        else:
            found.append((place, value))
    return found, left


def place_values(requests: dict[str, dict[Place, str]], numbers: dict[Place, bool] | None = None,
                 ) -> tuple[Place, dict[str, str | int]] | None:
    """The one field of a board's requests that changes with the place, and the value it holds for each place, found by
    code from the requests the picked places made (`requests`: the place typed -> the fields of its request,
    `leaves`). The field must be in every request, hold a different value in each of them, and be the only one that
    does; at least two places are needed to tell it from noise. `numbers` says which fields are JSON numbers (the value
    is then a number). None when no such single field is found."""
    words = [w for w in requests if requests[w]]
    if len(words) < 2:
        return None
    common = set.intersection(*(set(requests[w]) for w in words))
    moving = [k for k in sorted(common) if len({requests[w][k] for w in words}) == len(words)]
    if len(moving) != 1:
        return None
    field = moving[0]
    mapping: dict[str, str | int] = {}
    for w in words:
        raw = requests[w][field]
        mapping[place_key(w)] = int(raw) if (numbers or {}).get(field) and raw.lstrip("-").isdigit() else raw
    return field, mapping


# --- what a recipe holds ----------------------------------------------------------------------------------------------


def blanks_in(value: Any) -> set[str]:
    """The blanks (`phrase`, `place`) a text, or the texts of a list or object, hold."""
    if isinstance(value, str):
        return set(_BLANK.findall(value))
    if isinstance(value, dict):
        return {n for v in value.values() for n in blanks_in(v)}
    if isinstance(value, list):
        return {n for v in value for n in blanks_in(v)}
    return set()


def blank_fields(url: str, params: dict | None, body: Any) -> dict[str, set[Place]]:
    """Where each blank sits in a recipe: the fields (`leaves`) of its address's query, params and body that hold it."""
    out: dict[str, set[Place]] = {"phrase": set(), "place": set(), "since": set()}
    for k, v in leaves(url, body, params).items():
        for name in _BLANK.findall(v):
            out[name].add(k)
    return out


def literals_left(url: str, params: dict | None, body: Any, typed: dict | None) -> list[str]:
    """The typed words that remain whole in a recipe's address (its path and query, not its host), params or body.
    Whole: not inside a longer word. Case is ignored and an encoded word is decoded; a blank is not a typed word."""
    words = literals_of(typed)
    if not words:
        return []
    split = urlsplit(url)
    texts = [split.path, *leaves(url, body, params).values()]
    texts = [unquote_plus(_ANY_BRACES.sub(" ", t)) for t in texts] + [_ANY_BRACES.sub(" ", t) for t in texts]
    found = []
    for word in words:
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", re.I)
        if any(pattern.search(t) for t in texts):
            found.append(word)
    return found
