"""HTML recipes, running (docs/PRD.md path 49): a site whose postings are in the page's own HTML is read by selectors
recorded once from the rendered page, run on the page the app's own polite fetcher gets, with no browser at all
(`sites/skills.HtmlRecipe` for a site, `HtmlPostingRecipe` for one employer host's posting pages).

**Reading a list** (`run`, called by `skills.run_recipe`). The start page is fetched (robots.txt asked first by the
fetcher, about a request a second to a host, the honest user agent, no cookie), the recipe's `links`
anchors are read with their rows' `location` and `date`, and the `next` anchor is followed while it gives a public
address on the careers host that is not read yet. A refusal (401, 403, 429), a bot check (a 202 document, or a short
page that says so) and robots.txt disallowing a page are stops (`SkillError` with the status that cools the site, or
`Refused`), never worked around. What a refusal means is a small table of data (`sites/refusals.py`, PRD 37.17): 429 and
503 rest the host and ask again (three in a row end the run), a sign-in page skips that search, a bot check, 401 and 403
stop the host, and a page that says there are no results is an empty read, not a failure. Nothing is typed or clicked.
A link is kept only by the rules every page recipe keeps (`page_recipe._kept`: a public address on the careers host,
no sign-in or application, posting-shaped). The walk ends on a page that brings nothing new or when the night's time
runs out; both are said and the listing is marked
truncated, so it closes nothing.

**Searching** (`HtmlRecipe.search_url`). A recipe that has one is read for each of the owner's titles (and places)
from Search settings, read by the caller each night and never stored: the address is filled with the quoted word, the
results pages are walked like any list, and the postings of all the searches are kept once. What a search shows is a
partial view of the board, so the listing is truncated and closes nothing. With no title, the start page is read as
before.

**A fragment** (`HtmlRecipe.fragment_url`). A board whose page asks a script endpoint for a small piece of HTML that
holds the cards is read the same way: the endpoint's address is filled with the owner's word, fetched plainly, and the
piece parsed with the recipe's selectors. Its `{page}` (from 1) or `{offset}` (the cards read before it) pages the
search, at most `FRAGMENT_PAGES` pages for a title and place, and an answer with no card ends it.

**Who a card is** (`company`, `job_id`). Inside the card the recipe may select the employer's name and the board's id
for the posting. The role is the employer's named there (the night's read files it under that company, and under the
board's name only when the card names none), and the id is what a posting is told apart by: two cards of one id, on
one page or two, are one posting.

**Reading a posting** (`read_text`). The posting's page is fetched the same way and the recipe's `text` element
read; the page's HTML is kept as the raw copy. A page the recipe does not cover (off its careers host, or a host
recipe whose `url_pattern` does not match it whole) is not fetched. Whether the text is a whole posting is the
caller's to judge (`whole.why_not`). A host recipe that is `jsonld` reads no element: the text is the
description of the schema.org JobPosting the page embeds (`jsonld_text`), for a page whose HTML holds no text of its
own."""

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from app.jobsearch.adapters import Listing, _loose_date, _posting, job_posting, page_facts, page_text, stopped_at
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, public_url, timeup
from app.jobsearch.postings import closed_words
from app.sites import added, refusals, search_fill, skills
from app.sites import html_select as hs
from app.timeutil import owner_today

log = logging.getLogger(__name__)

#: A document the site answered with that is a bot check and not a page (an AWS WAF challenge answers 202 and no body),
#: and the status such a stop is given, so the site rests as it does for any refusal.
CHALLENGE_STATUSES = (202,)
BLOCK_STATUS = 403
#: The most result pages one read walks (a guard against a next link that never ends).
PAGES_AT_MOST = 400
#: The most pages of a fragment's search read for one title and place (the endpoint has no next link to end it).
FRAGMENT_PAGES = 5
#: The attributes that may hold a card's own id, in the order they are tried; the element's words come after them.
_ID_ATTRS = ("data-job-id", "data-id", "data-jobid", "id")
_ID_OK = re.compile(r"^[\w.:/@-]{1,100}$")
PAGE_REPEATED = "a page repeated one already read"
TIME_RAN_OUT = "the time allowed for the read ran out"


@dataclass
class Walk:
    """What a walk of the list pages found: the rows kept (by address), the page-one count, and why it stopped early.
    `searched`: the pages read were the site's search results for the owner's words, a partial view of the board."""

    found: dict[str, dict] = field(default_factory=dict)
    first_rows: int = 0
    pages: int = 0
    stopped: str | None = None
    searched: bool = False
    #: What the run's answers said (`sites/refusals.py`): its empty and skipped searches. None for a walk made by hand.
    guard: refusals.Guard | None = None
    #: The places the search names that the recipe's place map has no value for (not searched, said in the note).
    unmapped: list[str] = field(default_factory=list)


async def fetch_page(fetch: Fetcher, url: str) -> tuple[str, bytes]:
    """A page's HTML and bytes, or a stop (`checked`). `polite.Refused` (robots.txt, a private address) is not
    caught."""
    return checked(await fetch.get(url), url)


async def _list_page(fetch: Fetcher, guard: refusals.Guard, url: str) -> tuple[str, bytes, str | None]:
    """A list page's HTML, its bytes and the address it came from, or a stop. The refusal table (`guard`) comes first: a
    rest (429, 503) slows the host and asks again, and a stop (401, 403) stops the host until the owner's next off
    hours; a bot check `checked` finds is a stop of the same kind."""
    reply = await fetch.get(url)
    while await guard.status(reply.status_code) == refusals.REST:
        reply = await fetch.get(url)
    try:
        html, raw = checked(reply, url)
    except skills.SkillError as exc:
        if exc.status == BLOCK_STATUS:
            raise await guard.halt(BLOCK_STATUS, str(exc)) from exc
        raise
    return html, raw, refusals.final_url(reply)


def checked(reply, url: str) -> tuple[str, bytes]:
    """A reply's HTML and bytes, or a stop: SkillError with the status of a refusal or a bot check (403 for a check), or
    the status of any answer that is not a page."""
    host = urlsplit(url).hostname
    if reply.status_code in CHALLENGE_STATUSES:
        raise skills.SkillError(f"{host} showed a bot check (answered {reply.status_code})", BLOCK_STATUS)
    if reply.status_code != 200:
        raise skills.SkillError(f"{host} answered {reply.status_code}", reply.status_code)
    if recipe_record.looks_walled(reply.text):
        raise skills.SkillError(f"{host} showed a bot check", BLOCK_STATUS)
    return reply.text, reply.content


def _first_line(node: hs.Node) -> str:
    for source in (node.text(), node.get("aria-label") or "", node.get("title") or ""):
        for line in source.split("\n"):
            if line.strip():
                return line.strip()[:300]
    return ""


def _within(row: hs.Node | None, selector: str | None) -> hs.Node | None:
    return hs.select_one(row, selector) if row is not None and selector else None


def _said(node: hs.Node | None) -> str | None:
    """What a date or a place element says: its `datetime` attribute (a `time` element) or its words."""
    if node is None:
        return None
    return " ".join((node.get("datetime") or node.text()).split())[:200] or None


def ident_of(node: hs.Node | None) -> str | None:
    """A card's own id: the first of its `data-job-id`, `data-id`, `data-jobid` and `id` attributes, else its words;
    None when it holds none or what it holds is not a plain id."""
    if node is None:
        return None
    for value in [*(node.get(a) for a in _ID_ATTRS), " ".join(node.text().split())]:
        if value and _ID_OK.match(value.strip()):
            return value.strip()
    return None


def _card_part(row: hs.Node | None, selector: str | None) -> hs.Node | None:
    """The element a `company` or `job_id` selector names in a card: inside it, or the card itself."""
    if row is None or not selector:
        return None
    return hs.select_one(row, selector) or (row if hs.matches(row, selector) else None)


def apply_of(node: hs.Node | None, page_url: str, base: str) -> str | None:
    """The employer's own address an `apply` element holds: the first of its `skills.APPLY_ATTRS` attributes that is one
    (`skills.employer_address`: public https, not the board's and not another board's). Read from the card, never
    opened."""
    if node is None:
        return None
    for name in skills.APPLY_ATTRS:
        if (value := node.get(name)) and (found := skills.employer_address(urljoin(page_url, value.strip()), base)):
            return found
    return None


def rows_of(root: hs.Node, page_url: str, recipe: skills.HtmlRecipe) -> list[dict]:
    """The rows `recipe` reads on one page, kept by `page_recipe._kept`: {href, title, location, posted, company,
    job_id} and, when the recipe's `apply` finds an employer's address in the card, `apply`."""
    out: list[dict] = []
    for a in hs.select(root, recipe.links):
        href = urljoin(page_url, a.get("href") or "") if a.get("href") else ""
        if not href.startswith(("http://", "https://")):
            continue
        row = hs.closest(a, recipe.row) if recipe.row else None
        place = _said(_within(row, recipe.location))
        kept = page_recipe._kept({"href": href, "title": _first_line(a), "location": place}, recipe.url)
        if kept is None:
            continue
        kept["posted"] = _loose_date(_said(_within(row, recipe.date)))
        kept["company"] = _said(_within(row, recipe.company))
        kept["job_id"] = ident_of(_card_part(row, recipe.job_id)) if recipe.job_id else None
        if recipe.apply and (employer := apply_of(_card_part(row, recipe.apply), page_url, recipe.url)):
            kept["apply"] = employer
        out.append(kept)
    return out


def next_page(root: hs.Node, page_url: str, recipe: skills.HtmlRecipe, visited: set[str]) -> str | None:
    """The next page's address: the `next` anchor's own address when it is public, on the careers host and not read."""
    if not recipe.next or (anchor := hs.select_one(root, recipe.next)) is None or not anchor.get("href"):
        return None
    href = urljoin(page_url, anchor.get("href") or "").split("#", 1)[0]
    if href in visited or not public_url(href) or not page_recipe.on_careers_host(href, recipe.url):
        return None
    return href


def searches_of(recipe: skills.HtmlRecipe, search: search_fill.Search | None) -> list[tuple[str | None, str | None]]:
    """The searches a read makes, as (title, place): with a `search_url` and at least one title, each title and, when
    the address has a `{place}`, each place (none: one search with no place); otherwise one read of the start page
    (None, None)."""
    if not recipe.template() or search is None or not search.phrases:
        return [(None, None)]
    places, _left = skills.place_searches(search, "place" in skills.placeholders_of(recipe.template()),
                                          recipe.place_map)
    return [(phrase, place) for phrase in search.phrases for place in places]


def unmapped_of(recipe: skills.HtmlRecipe, search: search_fill.Search | None) -> list[str]:
    """The places the search names that the recipe's place map has no value for."""
    if not recipe.template() or search is None or not search.phrases:
        return []
    return skills.place_searches(search, "place" in skills.placeholders_of(recipe.template()), recipe.place_map)[1]


def since_of(recipe: skills.HtmlRecipe, search: search_fill.Search | None) -> str | None:
    """What the search address's `{since}` is filled with for this read (`search_fill.since_value`), None when it has
    none or the recipe holds no date window."""
    return search_fill.since_value(recipe.since, search) if recipe.since is not None else None


def start_of(recipe: skills.HtmlRecipe, phrase: str | None, place: str | None, since: str | None = None) -> str:
    """The address one search starts from: the start page, or the search's results for this title, place and date
    window (a public address, or SkillError)."""
    if phrase is None or not recipe.template():
        return recipe.url
    url = skills.search_address(recipe.template() or "", phrase, place, since=since)
    if not public_url(url):
        raise skills.SkillError("the address the recipe makes with the search's words is not a public address")
    return url


async def walk(fetch: Fetcher, recipe: skills.HtmlRecipe, *, enough: int | None = None, pages: int | None = None,
               search: search_fill.Search | None = None) -> Walk:
    """Read the list pages from the start. `enough` stops it once that many postings are found (a recording's test needs
    no more), `pages` after that many pages in all. With `search` and a recipe that has a `search_url`, the pages of the
    search's results are read for each title (and place) in turn, the postings of all of them kept once."""
    got = Walk()
    where = urlsplit(recipe.url).hostname or ""
    got.guard = guard = refusals.Guard(refusals.table_for(recipe.refusals, added.refusals_of(where)), where,
                                       skills.SkillError, fetch)
    wanted = searches_of(recipe, search)
    got.unmapped = unmapped_of(recipe, search)
    since = since_of(recipe, search)
    for i, (phrase, place) in enumerate(wanted):
        if i and (timeup(fetch) or (enough is not None and len(got.found) >= enough)
                  or (pages is not None and got.pages >= pages)):
            break
        got.searched = got.searched or phrase is not None
        try:
            await _walk_pages(fetch, recipe, got, start_of(recipe, phrase, place, since), enough, pages,
                              (phrase, place), guard, since)
        except refusals.SkipSearch:
            continue  # the site sent this search to a sign-in page: the other searches are read
    if not got.found and (why := guard.skipped_all(len(wanted))):
        raise guard.fail(why, 401)  # a refusal that read nothing: the site rests, the recipe is not bad
    return got


def fragment_next(recipe: skills.HtmlRecipe, search: tuple[str | None, str | None], read: int, cards: int,
                  visited: set[str], since: str | None = None) -> str | None:
    """The next page of a fragment's search: its address with `{page}` or `{offset}` moved on, when the address pages,
    the search's page limit is not reached and the address is public and not read. None otherwise."""
    held = skills.placeholders_of(recipe.fragment_url) if recipe.fragment_url else set()
    phrase, place = search
    if not held & {"page", "offset"} or phrase is None or read >= FRAGMENT_PAGES:
        return None
    url = skills.search_address(recipe.fragment_url or "", phrase, place, page=read + 1, offset=cards, since=since)
    return url if url not in visited and public_url(url) else None


async def _walk_pages(fetch: Fetcher, recipe: skills.HtmlRecipe, got: Walk, first: str, enough: int | None,
                      pages: int | None, search: tuple[str | None, str | None] = (None, None),
                      guard: refusals.Guard | None = None, since: str | None = None) -> None:
    """The pages of one search, from its first address, added to `got`."""
    visited: set[str] = set()
    cards = 0
    url: str | None = first
    read = 0
    while url:
        visited.add(url)
        final: str | None = None
        if guard is None:
            html, _raw = await fetch_page(fetch, url)
        else:
            html, _raw, final = await _list_page(fetch, guard, url)
        got.pages += 1
        read += 1
        root = hs.parse(html)
        before = len(got.found)
        listed = 0
        for row in rows_of(root, url, recipe):
            listed += 1
            got.found.setdefault(f"id:{row['job_id']}" if row.get("job_id") else row["href"], row)
        if guard is not None:
            await guard.page(html, final, url, listed, first=read == 1)
        cards += listed
        if got.pages == 1:
            got.first_rows = listed
        if enough is not None and len(got.found) >= enough:
            break
        if pages is not None and got.pages >= pages:
            break
        url = next_page(root, url, recipe, visited) or (
            fragment_next(recipe, search, read, cards, visited, since) if listed else None)
        if url is None:
            break
        if listed and len(got.found) == before:
            got.stopped = stopped_at(len(got.found), PAGE_REPEATED)
            break
        if timeup(fetch):
            got.stopped = stopped_at(len(got.found), TIME_RAN_OUT)
            break
        if read >= PAGES_AT_MOST:
            got.stopped = stopped_at(len(got.found), f"{PAGES_AT_MOST} pages were read")
            break


def company_of(posting: dict, company: str | None) -> dict:
    """The posting with the employer its card names, when the card names one (the night's read files a role under it,
    and under the board's own name only when there is none)."""
    if company:
        posting["company"] = company
    return posting


async def run(fetch: Fetcher, recipe: skills.HtmlRecipe, **limits) -> Listing:
    """Read the site by its HTML recipe. Raises `SkillError` (a refusal, a bot check or a page that is not served) and
    `polite.Refused` (robots.txt)."""
    got = await walk(fetch, recipe, **limits)
    postings: list[dict] = [
        company_of(_posting(raw=row, ext_id=row.get("job_id") or page_recipe.ext_id_of(row["href"]),
                            title=row["title"], location=row["location"], posted=row["posted"], url=row["href"],
                            apply_url=row.get("apply") or row["href"]), row.get("company"))
        for row in got.found.values()]
    skipped = got.guard.skipped if got.guard else []
    note = "; ".join(x for x in (got.stopped, f"{len(skipped)} searches were sent to a sign-in page and left out"
                                  if skipped else None, skills.unmapped_note(got.unmapped)) if x) or None
    listing = Listing(postings, truncated=got.stopped is not None or got.searched or bool(skipped), note=note)
    return got.guard.answer(listing) if got.guard else listing


@dataclass
class HtmlRead:
    """One posting page read: the status the site answered with, the text of the recipe's element (None when the
    element is not on the page), the page's bytes, and `gone`: what the page says when it says the posting is closed
    (`closed_note`), in which case the text is not a live posting's."""

    status: int
    text: str | None
    raw: bytes
    gone: str | None = None


def covers(recipe: skills.HtmlRecipe | skills.HtmlPostingRecipe, link: str) -> bool:
    """Whether the recipe reads this posting address: a public one on the careers host of a site recipe's start page,
    or one a host recipe's host covers and its `url_pattern` matches whole."""
    if not public_url(link):
        return False
    if isinstance(recipe, skills.HtmlRecipe):
        return page_recipe.on_careers_host(link, recipe.url)
    host = urlsplit(link).hostname or ""
    return skills.host_covers(recipe.host, host) and skills.compile_pattern(
        recipe.url_pattern, "url_pattern").fullmatch(link.split("#", 1)[0]) is not None


def closed_note(link: str, html: str, root: hs.Node, inside: hs.Node | None) -> str | None:
    """What a page says when it says its posting is closed, or None: the posting's own `validThrough` that has passed
    (as Go reads it, `go_checks._past`), or a closed phrase (`closed_words.py`, the one list) in the words of the page
    outside the posting's own element `inside`, so the posting's words never end it. `root` is the page parsed, and is
    changed here."""
    from app.jobsearch.browser import go_checks  # it reads the whole job-search side

    if day := go_checks._past((job_posting(html) or {}).get("validThrough"), owner_today()):
        return f"{link}: the posting's own data says it took applications until {day:%d %B %Y}."
    if inside is not None and inside.parent is not None:
        inside.parent.children.remove(inside)
    said = closed_words.found(root.text())
    return f'{link} says "{said}".' if said else None


def jsonld_text(html: str) -> str | None:
    """The posting a page embeds as a schema.org JobPosting in a `ld+json` script, as the app reads one everywhere
    (`adapters.page_text`: its description with the markup gone, behind the page's own header lines), or None when the
    page embeds none or its JobPosting has no description. Whether it is a whole posting is the caller's to judge."""
    return page_text(html) if page_facts(html).get("text") else None


async def read_text(fetch: Fetcher, recipe: skills.HtmlRecipe | skills.HtmlPostingRecipe, link: str,
                    ) -> HtmlRead | None:
    """A posting's text by the recipe's `text` element (or, for a host recipe that is `jsonld`, the JobPosting the page
    embeds), or None when the recipe has none or does not cover the address. A page the site answered 404 or 410 to
    comes back with its status and no text (a posting that is gone); another status that is not a page is a stop
    (`fetch_page`)."""
    jsonld = isinstance(recipe, skills.HtmlPostingRecipe) and recipe.jsonld
    if not (recipe.text or jsonld) or not covers(recipe, link):
        return None
    reply = await fetch.get(link)
    if polite.gone(link, reply.status_code) is not None:
        return HtmlRead(reply.status_code, None, b"")
    html, raw = checked(reply, link)
    root = hs.parse(html)
    if jsonld:
        return HtmlRead(200, jsonld_text(html), raw, closed_note(link, html, root, None))
    assert recipe.text is not None
    node = hs.select_one(root, recipe.text)
    text = node.text() if node is not None else None
    return HtmlRead(200, text, raw, closed_note(link, html, root, node))
