"""How a learned board's refusals and empty pages are told apart, as data (docs/PRD.md 37.17).

A site the learner reads by code (a `request`, `html` or `page` recipe) answers a reader in four ways that look alike
from outside: a page of results, a page that says there are none, a refusal, and a page the app cannot make sense of.
The first two are a good night. The third is the site saying no, and what it means depends on how it says it. The
fourth is a recipe that no longer fits. This module holds what tells them apart, as a small closed table, so a night
never mistakes an empty page for a bad night, nor a refusal for an empty page:

- `statuses`: an HTTP status to one of three classes. `rest`: back off (the host's pace doubles, to
  `REST_CAP_SECONDS`) and ask again, and `REST_RUN_ENDS` rests in a row end the run. `skip`: leave this search, read the
  others. `stop`: end the run and stop the host until the owner's next off hours (`host_stops.record_fetch_refusal`).
- `signin`: the words of a sign-in page (and, by its address, a redirect to one). `challenge`: the words of a bot
  check. `empty`: the words of a results page that says there is nothing.

The defaults are generic and shared, in this file (`DEFAULT_STATUSES`, `SIGNIN_WORDS`, `CHALLENGE_WORDS`,
`EMPTY_WORDS`): 429 and 503 rest, a redirect to a sign-in page or its words skip the search, a bot check, 401 and 403
stop. A recipe (`refusals`) and the site's polite record (`js_sites.polite`, `refusals`) may carry a table of their own
(`sanitize`): a recording may add the page words it saw. **A learned value only ever makes the app more careful**
(`table_for`): a status the defaults treat gently may be raised (rest to skip to stop) and never lowered, a status the
defaults do not name may be added, and a word may be added to a list and never taken out of one. The page words are
looked for only on a page with no postings, so a real results page with a "Sign in" link in its header is never
mistaken for a sign-in page. A page with no postings is judged in this order: a bot check, a sign-in page, an empty
results page, so nothing that looks like a refusal is ever counted as empty.

Standard library only at import time (the polite layer imports `sites.added`, which imports this module); the consent
filter and the host-stop record are reached inside the functions that need them."""

import html as html_lib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: What a refusal is told to do (the classes of the table), least careful first.
REST, SKIP, STOP = "rest", "skip", "stop"
CLASSES = (REST, SKIP, STOP)
_RANK = {REST: 1, SKIP: 2, STOP: 3}

#: What a page that has no postings in it turned out to be (`Guard.page`): the site said there are none, or nothing
#: explains it (the read goes on as it always did: no postings read).
OK, EMPTY, OTHER = "ok", "empty", "other"

#: The defaults, as the owner's own hand-built reader has them: the site rests the reader on these, ends the run on
#: these. A recipe or a site's record can raise one of them and never lower it.
DEFAULT_STATUSES: dict[int, str] = {429: REST, 503: REST, 401: STOP, 403: STOP}
#: The statuses no table may name: the ones that already say a page is gone.
_NEVER = frozenset({404, 410})

#: Seconds a rest never lets the pace go past, and how many rests in a row end the run.
REST_CAP_SECONDS = 240.0
REST_RUN_ENDS = 3

#: Words (lower case, single spaces) a page with no postings may say: a bot check, a sign-in page, or a results page
#: with nothing in it. A bot check is only looked for in a page shorter than `PAGE_LIMIT`.
CHALLENGE_WORDS = ("captcha", "just a moment", "verify you are human", "verify that you are human",
                   "verify you are a human", "verify that you are a human", "are you a robot", "unusual traffic",
                   "access denied", "px-captcha", "cf-chl", "attention required", "request blocked", "bot detection")
SIGNIN_WORDS = ("sign in to continue", "log in to continue", "please sign in", "please log in", "sign in to view",
                "log in to view", "sign in to see", "log in to see", "you must be signed in", "you must be logged in",
                "you need to sign in", "you need to log in")
EMPTY_WORDS = ("no results", "no jobs found", "no jobs match", "no matching jobs", "no matching results",
               "no positions found", "no open positions", "no openings", "0 results", "0 jobs found",
               "did not match any", "didn't match any", "couldn't find any", "could not find any", "no vacancies",
               "nothing matched", "no listings")
#: A read that was sent on to an address with one of these as a whole path part was sent to a sign-in page.
SIGNIN_PATHS = frozenset({"login", "signin", "sign-in", "sign_in", "sso", "auth", "authwall", "authenticate"})
PAGE_LIMIT = 60_000

#: The most a learned table holds: statuses, words in a list, and the length of a word.
STATUSES_MOST, WORDS_MOST, WORD_MIN, WORD_MAX = 8, 12, 4, 80
_KEYS = frozenset({"statuses", "signin", "challenge", "empty"})
_SCRIPTS = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.I | re.S)
_TAGS = re.compile(r"<[^>]*>")
#: What a listing carries when the site said there are none (`Guard.answer`, `said`).
SAID_EMPTY = "said_empty"


class SkipSearch(Exception):
    """The site sent this search to a sign-in page or told the reader to skip it: leave the search, read the others."""


def words_of(text: str) -> str:
    """The words of a page as a reader sees them: scripts, styles and tags out, entities read, lower case, one space
    between words."""
    plain = _TAGS.sub(" ", _SCRIPTS.sub(" ", text or ""))
    return " ".join(html_lib.unescape(plain).casefold().split())


def _word(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} holds words, as text")
    word = " ".join(value.casefold().split())
    if not WORD_MIN <= len(word) <= WORD_MAX:
        raise ValueError(f"a word in {what} is {WORD_MIN} to {WORD_MAX} characters")
    return word


def _status(key: Any) -> int:
    try:
        number = -1 if isinstance(key, bool) else int(key)
    except (TypeError, ValueError):
        number = -1
    if not 400 <= number <= 599 or number in _NEVER:
        raise ValueError(f"{key!r} is not a status a refusal can have (400 to 599, never 404 or 410)")
    return number


def sanitize(raw: Any) -> dict[str, Any]:
    """A refusal table as data, checked: `{statuses: {code: class}, signin: [words], challenge: [words], empty:
    [words]}`, every key optional. Raises ValueError for anything else (an unknown key, a status outside 400 to 599, a
    class that is not rest, skip or stop, too many entries). Returns the table with its statuses as strings and its
    words lower case, without repeats; None or an empty object is an empty table."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("refusals is an object")
    if extra := set(raw) - _KEYS:
        raise ValueError(f"refusals has no {sorted(map(str, extra))[0]!r}: only statuses, signin, challenge and empty")
    out: dict[str, Any] = {}
    statuses = raw.get("statuses")
    if statuses:
        if not isinstance(statuses, Mapping) or len(statuses) > STATUSES_MOST:
            raise ValueError(f"statuses maps at most {STATUSES_MOST} statuses to a class")
        kept: dict[str, str] = {}
        for key, cls in statuses.items():
            if cls not in CLASSES:
                raise ValueError(f"a status is told to be one of {', '.join(CLASSES)}, not {cls!r}")
            kept[str(_status(key))] = cls
        out["statuses"] = kept
    for name in ("signin", "challenge", "empty"):
        listed = raw.get(name)
        if listed:
            if not isinstance(listed, (list, tuple)) or len(listed) > WORDS_MOST:
                raise ValueError(f"{name} is a list of at most {WORDS_MOST} words")
            out[name] = list(dict.fromkeys(_word(w, name) for w in listed))
    return out


def lenient(raw: Any) -> dict[str, Any]:
    """`sanitize`, for a record written long ago or by something else: the empty table when it is not valid, never an
    error (a night's read is never stopped by a bad record)."""
    try:
        return sanitize(raw)
    except ValueError:
        return {}


@dataclass(frozen=True)
class Table:
    """The table a read goes by: status to class, and the words to look for."""

    statuses: Mapping[int, str] = field(default_factory=lambda: dict(DEFAULT_STATUSES))
    signin: tuple[str, ...] = SIGNIN_WORDS
    challenge: tuple[str, ...] = CHALLENGE_WORDS
    empty: tuple[str, ...] = EMPTY_WORDS


DEFAULT = Table()


def table_for(*learned: Any) -> Table:
    """The defaults made stricter by each learned table in turn (a recipe's, a site's record): a learned status can only
    be raised above what the table has (rest, skip, stop) or added; a learned word is added to its list; a learned empty
    word is kept only when it is a phrase (two words or more) that no refusal word of the table holds, so a refusal's
    own words never make a page empty. Nothing learned removes or lowers anything of the defaults."""
    statuses = dict(DEFAULT_STATUSES)
    signin: list[str] = list(SIGNIN_WORDS)
    challenge: list[str] = list(CHALLENGE_WORDS)
    empty: list[str] = list(EMPTY_WORDS)
    pending: list[str] = []
    for one in learned:
        table = lenient(one)
        for key, cls in (table.get("statuses") or {}).items():
            code = int(key)
            if _RANK[cls] > _RANK.get(statuses.get(code, ""), 0):
                statuses[code] = cls
        signin += [w for w in table.get("signin", ()) if w not in signin]
        challenge += [w for w in table.get("challenge", ()) if w not in challenge]
        pending += table.get("empty", ())
    for word in pending:
        phrase = len(word.split()) >= 2
        clash = any(r in word or word in r for r in (*signin, *challenge))
        if phrase and not clash and word not in empty:
            empty.append(word)
    return Table(statuses, tuple(signin), tuple(challenge), tuple(empty))


def combine(first: Any, second: Any) -> dict[str, Any]:
    """Two learned tables as one that is at least as careful as each: statuses at the higher class, words of both. What
    an overlay keeps for a host that two rows of the site list share."""
    one, two = lenient(first), lenient(second)
    out: dict[str, Any] = {}
    statuses = dict(one.get("statuses") or {})
    for key, cls in (two.get("statuses") or {}).items():
        if _RANK[cls] > _RANK.get(statuses.get(key, ""), 0):
            statuses[key] = cls
    if statuses:
        out["statuses"] = statuses
    for name in ("signin", "challenge", "empty"):
        words = list(dict.fromkeys((*one.get(name, ()), *two.get(name, ()))))
        if words:
            out[name] = words
    return out


def signin_address(final: str | None, asked: str | None) -> bool:
    """Whether a read that was sent on to `final` was sent to a sign-in page: the address it ended at has a sign-in word
    as a whole path part, and the address it asked for did not."""

    def parts(address: str | None) -> set[str]:
        return {p.casefold() for p in urlsplit(address or "").path.split("/") if p} & SIGNIN_PATHS

    return bool(final) and bool(parts(final) - parts(asked))


def challenged(text: str, table: Table = DEFAULT) -> bool:
    """Whether a page is a bot check: it is short (a real results page is not) and says one of the table's words once
    its cookie and consent banners are taken out (a banner's own list of cookies is not a check)."""
    from app.jobsearch import consent

    if len(text or "") >= PAGE_LIMIT:
        return False
    page = consent.without_banners(text).casefold()
    return any(word in page for word in table.challenge)


def signin_words(text: str, table: Table = DEFAULT) -> bool:
    page = words_of(text)
    return any(word in page for word in table.signin)


def said_empty(text: str, table: Table = DEFAULT) -> bool:
    page = words_of(text)
    return any(word in page for word in table.empty)


def final_url(reply: Any) -> str | None:
    """The address a reply came from, after its redirects; None when it does not know (a reply made by hand)."""
    try:
        return str(reply.url)
    except Exception:
        return None


def said(listing: Any) -> bool:
    """Whether a listing with no postings is one the site said was empty (`Guard.answer`)."""
    return bool(getattr(listing, SAID_EMPTY, False))


@dataclass
class Rests:
    """The rests of one run: how many came one after another, with no good answer between them."""

    row: int = 0

    def rested(self) -> bool:
        """Note one more rest; whether it is the one that ends the run."""
        self.row += 1
        return self.row >= REST_RUN_ENDS

    def answered(self) -> None:
        self.row = 0


def wait_for(rests: int, base: float) -> float:
    """The seconds the nth rest in a row waits when the read has no pace of its own to double (a page in the app's own
    Chrome): `base` doubled for each rest, to at most `REST_CAP_SECONDS`."""
    return min(max(base, 1.0) * 2 ** max(rests, 1), REST_CAP_SECONDS)


class Guard:
    """One run's view of the table: every answer of the run is put to it, and it says what to do. It raises `error` (the
    runner's own, `skills.SkillError`, marked `refused`) for the end of a run and `SkipSearch` for the end of one
    search.
    It counts what the run saw: `empty` searches, `skipped` ones, `unexplained` first pages with no postings and no word
    that explains them, so `answer` can tell a run that read nothing because the site said so.

    `fetch` is the polite fetcher, when the read has one: a rest slows its host (`slow_host`), and a stop ends the
    host's turn for the run (`refused_hosts`)."""

    def __init__(self, table: Table, host: str, error: Callable[..., Exception], fetch: Any = None) -> None:
        self.table, self.host, self.error, self.fetch = table, host, error, fetch
        self.rests = Rests()
        self.empty = 0
        self.skipped: list[str] = []
        self.unexplained = 0

    def fail(self, message: str, status: int | None) -> Exception:
        exc = self.error(message, status)
        exc.refused = True  # type: ignore[attr-defined]  # a refusal is a stop, never a bad night (`recipes.blocked`)
        return exc

    async def halt(self, status: int | None, why: str) -> Exception:
        """Stop the host until the owner's next off hours and end the run; the error to raise."""
        if hasattr(self.fetch, "refused_hosts"):
            self.fetch.refused_hosts.add(self.host)
        from app.jobsearch.fetch import host_stops

        await host_stops.record_fetch_refusal(self.host, status or 403)
        return self.fail(why, status or 403)

    async def status(self, status: int | None) -> str | None:
        """What to do with an answer's status. None: nothing for the table to say (the read goes on with it as it always
        did). `rest`: the host's pace was doubled and the same request is to be made again. Raises the run's error for
        `stop` (the host is stopped) and for the rest that ends the run, and `SkipSearch` for `skip`."""
        cls = self.table.statuses.get(status) if status is not None else None
        if cls is None:
            return None
        why = f"{self.host} answered {status}"
        if cls == STOP:
            raise await self.halt(status, why)
        if cls == SKIP:
            self.skipped.append(why)
            raise SkipSearch(why)
        if self.rests.rested():
            raise self.fail(f"{why}, {REST_RUN_ENDS} times in a row: the run ends", status)
        slow = getattr(self.fetch, "slow_host", None)
        if slow is not None:
            slow(self.host, 2.0, REST_CAP_SECONDS)
        return REST

    async def page(self, text: str, final: str | None, asked: str | None, rows: int, *, first: bool = True,
                   data_empty: bool = False) -> str:
        """What a page that answered 200 is. With postings it is a page of results (`ok`), whatever it says. With none
        it is, in this order: a bot check (the host is stopped), a sign-in page by its address or words (the search is
        skipped), a page that says there are none (`empty`), or a page the table cannot explain (`other`: the read goes
        on as it always did). `first` is the first page of a search: a later page with none is only the end of the
        results, and is never counted. `data_empty`: the answer was data with a proper, empty list of results, which
        says there are none as a page's words do."""
        if rows:
            self.rests.answered()
            return OK
        if challenged(text, self.table):
            raise await self.halt(403, f"{self.host} showed a bot check")
        if signin_address(final, asked) or signin_words(text, self.table):
            why = f"{self.host} sent the search to a sign-in page"
            self.skipped.append(why)
            raise SkipSearch(why)
        self.rests.answered()
        if data_empty or said_empty(text, self.table):
            self.empty += first
            return EMPTY
        self.unexplained += first
        return OTHER

    def skipped_all(self, searches: int) -> str | None:
        """Why a run that read no search to its end ended: every one of its `searches` was sent to a sign-in page."""
        if searches and len(self.skipped) >= searches:
            return f"every search was sent to a sign-in page ({self.skipped[0]})"
        return None

    def answer(self, listing: Any) -> Any:
        """The run's listing as the night reads it: one with no postings is marked as said empty when the site said so
        for every search it read, and is left as it is when anything else explains it or nothing does."""
        if not listing.postings and self.empty and not self.unexplained:
            setattr(listing, SAID_EMPTY, True)
        return listing
