"""HTML recipes, recording (docs/PRD.md path 49): a recording whose page made no data request a recipe could repeat goes
on here, with the page as the app's own Chrome rendered it, and ends in a recipe that reads the page *without* a browser
(`browser/recipes/html_run.py`), or in the plain statement that it cannot.

**Recording** (`record_by_code`, from `recipe_record.record_by_code`). For a site, the start page is loaded in a tab of
the app's Chrome and its rendered HTML read; the posting links are the group `page_recipe.link_groups` ranks first (an
id leaf, a posting word; not a team's, a category's or a place's page), each group's row and the `location` and `date`
inside the row, the next-page anchor and, on a few postings opened in the same tab, the posting's text element are
worked out from the HTML by the pure functions below (`list_candidates`, `row_fields`, `next_selector`,
`text_selector`). For
a host's posting page (no site row) the text element and the address pattern are. Nothing is clicked or typed, nothing a
page says is run, no model is asked.

**The search** (an added site's first recording, `search_url`). When the recording typed the owner's title and the
address bar then held it in its query, code made a template of that address (`recipe_record.search_template`, no
model): the recipe keeps it as `search_url` only when a plain fetch of the search for the typed words (the first title
and place) reads postings by the recipe's selectors, and never with a typed word left in it (`refuse_literals`).

**The fragment** (`fragment_url`). A board whose page asks a script endpoint for a small piece of HTML holding the
cards leaves that answer in the recording (`recipes.sanitize` keeps it as a fragment, bounded). Code makes a template
of the address it was asked at (`recipe_record.fragment_template`: the typed words become `{phrase}` and `{place}`, a
first page's number `{page}` or `{offset}`), and the recipe keeps it as `fragment_url` only when two proofs hold:
offline (`fragment_problems`), every blank sits where the typed words were (the address filled with them is the address
the page asked at), the recipe's selectors read the recorded piece and no typed word is left in it; and live, a plain
fetch of the search reads postings, as for `search_url`. The selectors are the rendered start page's, or, when the start
page holds no cards for a plain fetch, those the piece's own cards make. A board that wants a place id (the typed place
is nowhere in the request) is searched by title alone, and its status says so (`recipe_record.PLACE_UNSUPPORTED`).

**Verifying** (`verify_list`, `verify_posting`). The recipe is run for real on what a *plain fetch* gets (the polite
fetcher: robots.txt, a request a second, an honest agent), not on the recording: it must read `VERIFY_ROWS` postings
and at least `PLAIN_SHARE` of the rows the rendered first page held, and its text element must give a whole posting on
a posting opened by plain fetch. A page that fails this draws what it shows by script: no recipe is kept, the recording
fails with a note that starts `NEEDS_JS` and says what the plain page held, and the site stays on the browser, unless
the owner added it: then the one chain link is made (`_chain`), a recording in the app's Chrome, once a site
(`recipe_live.CHAIN`). A list that reads but whose text does not is kept without its text. A refusal or a bot check
at any point is a block (the site rests), not a failed recording. Services flush; the caller commits."""

import logging
import math
import re
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.adapters import _common
from app.jobsearch.browser import code as bc
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.browser.recipes import interact as recipe_interact
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Refused, public_url
from app.jobsearch.postings import whole
from app.sites import added, recipes, same_roles, search_fill, skills, where
from app.sites import html_select as hs
from app.sites.models import Site, SiteRecipe, SiteRecording

log = logging.getLogger(__name__)

#: What a recording's note starts with when the page needs JavaScript to hold what it shows (PRD 49.4).
NEEDS_JS = "needs JavaScript:"
#: The share of the rendered first page's rows a plain fetch must also give (PRD 49.5).
PLAIN_SHARE = 0.5
#: How a recipe's provenance names how it was made (no model wrote it).
PROMPT_VERSION = "html-1"

_PLAIN_CLASS = re.compile(r"^[A-Za-z][\w-]{2,40}$")
_PLAIN_ID = re.compile(r"^[A-Za-z_][\w-]*$")
_NEVER = re.compile(recipe_interact.NEVER, re.I)
_LOCATION = re.compile(r"locat|city|place|region|country|office|geo|address", re.I)
_COMPANY = re.compile(r"compan|employer|organi[sz]ation|hiring|client", re.I)
_ID_ATTRS = ("data-job-id", "data-id", "data-jobid")
_DATE = re.compile(r"date|posted|ago|time", re.I)
_NEXT_WORDS = frozenset({"next", "next page", "next ›", "next »", "next >", "›", "»", ">", "→", "older"})
_NEXT_LABEL = re.compile(r"^\s*next( page| results)?\s*$", re.I)
_DESCRIPTION = ('[itemprop="description"]', '[data-automation-id*="description" i]', '[class*="job-description" i]',
                '[class*="jobdescription" i]', '[class*="description" i]', '[id*="description" i]', "article", "main",
                '[role="main"]')


# --- selectors from the rendered HTML ---------------------------------------------------------------------------


def _first(root: hs.Node, selector: str) -> hs.Node | None:
    try:
        return hs.select_one(root, selector)
    except ValueError:
        return None


def selector_for(root: hs.Node, node: hs.Node) -> str | None:
    """A selector whose first match on the page is `node`: its id, a stable attribute, a plain class, else its path with
    `:nth-of-type`. None when none holds."""
    tries: list[str] = []
    if _PLAIN_ID.match(node.get("id") or ""):
        tries.append(f"#{node.get('id')}")
    for attr in ("data-testid", "data-test", "data-automation-id", "rel", "aria-label", "name", "title"):
        v = node.get(attr)
        if v and len(v) < 80 and not re.search(r"[\"'\\\]]", v):
            tries.append(f'{node.tag}[{attr}="{v}"]')
    tries += [f"{node.tag}.{c}" for c in node.classes() if _PLAIN_CLASS.match(c) and not re.search(r"\d{3,}", c)]
    for t in tries:
        if _first(root, t) is node:
            return t
    anchor = node.parent  # a selector under the nearest ids, where the same class is the page's menu's first
    for _ in range(3):
        while anchor is not None and not _PLAIN_ID.match(anchor.get("id") or ""):
            anchor = anchor.parent
        if anchor is None:
            break
        for t in tries:
            if _first(root, f"#{anchor.get('id')} {t}") is node:
                return f"#{anchor.get('id')} {t}"
        anchor = anchor.parent
    parts: list[str] = []
    here: hs.Node | None = node
    while here is not None and here.tag != "#root" and len(parts) < 6:
        part = here.tag
        if _PLAIN_ID.match(here.get("id") or ""):
            parts.insert(0, f"#{here.get('id')}")
        else:
            if here.parent is not None and here.parent.seen.get(here.tag, 1) > 1:
                part += f":nth-of-type({here.index})"
            parts.insert(0, part)
        selector = " > ".join(parts)
        if _first(root, selector) is node:
            return selector
        if part.startswith("#"):
            break
        here = here.parent
    return None


def _row_selector(row: hs.Node) -> str:
    cls = next((c for c in row.classes() if _PLAIN_CLASS.match(c) and not re.search(r"\d{3,}", c)), None)
    return f"{row.tag}.{cls}" if cls else row.tag


def anchors_of(root: hs.Node, base: str) -> list[dict]:
    """The page's links on its own site, as `page_recipe.link_groups` takes them: {href, raw, title, row}."""
    home = page_recipe.careers_host(base)
    out: list[dict] = []
    for a in hs.select(root, "a"):
        raw = a.get("href") or ""
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        href = urljoin(base, raw)
        host = (urlsplit(href).hostname or "").lower().removeprefix("www.")
        title = html_recipe._first_line(a)
        if not (host == home or host.endswith("." + home)) or not 6 <= len(title) <= 160:
            continue
        if _NEVER.search(href) or _NEVER.search(title):
            continue
        row = a.parent
        while row is not None and row.tag not in ("li", "tr", "article", "#root"):
            row = row.parent
        out.append({"href": href, "raw": raw, "title": title,
                    "row": _row_selector(row) if row is not None and row.tag != "#root" else None})
        if len(out) >= 3000:
            break
    return out


def _key_of(el: hs.Node) -> str | None:
    """A selector for an element in a row that is the same for every row: its `itemprop`, a plain class or `time`."""
    if el.get("itemprop") and not re.search(r"[\"'\\\]]", el.get("itemprop") or ""):
        return f'[itemprop="{el.get("itemprop")}"]'
    hints = (_LOCATION, _DATE, _COMPANY)
    classes = [c for c in el.classes() if _PLAIN_CLASS.match(c) and not re.search(r"\d{3,}", c)]
    pick = next((c for c in classes if any(h.search(c) for h in hints)), classes[0] if classes else None)
    if pick:
        return f"{el.tag}.{pick}"
    return "time" if el.tag == "time" else None


def _apply_key(el: hs.Node, base: str) -> str | None:
    """A selector, the same for every row, of an element that holds the employer's own address in an attribute
    (`skills.APPLY_ATTRS`, an address `skills.employer_address` accepts): a `data-` attribute by its name, an anchor by
    its plain class. None when it holds none."""
    for name in skills.APPLY_ATTRS:
        if (value := el.get(name)) and skills.employer_address(value.strip(), base):
            if name.startswith("data-"):
                return f"[{name}]"
            return _key_of(el) if el.tag == "a" and el.classes() else None
    return None


def row_fields(root: hs.Node, links: str, row: str | None, base: str | None = None) -> dict:
    """The `location`, `date` and `company` selectors inside a row, when most rows hold such an element, `job_id`
    (the row itself) when most rows carry their own id in an attribute, and, given the page's address `base`, `apply`
    when most rows hold an element carrying the employer's own address: {location, date, company, job_id, apply}."""
    if not row:
        return {}
    pairs = [(a, hs.closest(a, row)) for a in hs.select(root, links)]
    rows = [(a, r) for a, r in pairs if r is not None]
    seen: dict[str, int] = {}
    applies: dict[str, int] = {}
    for a, r in rows:
        inside = {id(n) for n in a.elements()} | {id(a)}
        keys: set[str] = set()
        held: set[str] = set()
        for el in (r.elements() if base is None else [*r.elements(), r]):
            if base is not None and (key := _apply_key(el, base)):
                held.add(key)
            if id(el) in inside or el is r:
                continue
            if el.tag != "time" and any(isinstance(c, hs.Node) for c in el.children):
                continue  # only an element with words of its own, not one that holds others
            said = (el.get("datetime") or el.text()).strip()
            if not said or len(said) > 120:
                continue
            if key := _key_of(el):
                keys.add(key)
        for key in keys:
            seen[key] = seen.get(key, 0) + 1
        for key in held:
            applies[key] = applies.get(key, 0) + 1
    common = [k for k, n in seen.items() if len(rows) >= 2 and n >= 0.6 * len(rows)]
    out: dict = {}
    for key in common:
        if "location" not in out and _LOCATION.search(key):
            out["location"] = key
        elif "company" not in out and _COMPANY.search(key) and not _LOCATION.search(key):
            out["company"] = key
        elif "date" not in out and (key == "time" or _DATE.search(key)) and not _LOCATION.search(key):
            out["date"] = key
    if len(rows) >= 2 and sum(1 for _a, r in rows if any(r.get(a) for a in _ID_ATTRS)) >= 0.6 * len(rows):
        out["job_id"] = row  # the row itself holds the board's own id for the posting
    if len(rows) >= 2 and (kept := [k for k, n in applies.items() if n >= 0.6 * len(rows)]):
        out["apply"] = sorted(kept)[0]  # read from the card, never opened
    return out


def next_selector(root: hs.Node, base: str) -> str | None:
    """The selector of the next-page anchor: one that is `rel=next`, labelled "Next", or reads "Next" or an arrow; only
    an anchor with an address (the plain fetch cannot click)."""
    found: list[tuple[int, hs.Node]] = []
    for a in hs.select(root, "a"):
        href = a.get("href") or ""
        if not href or href.startswith(("#", "javascript:")):
            continue
        words = " ".join(a.text().lower().split())
        if "next" in (a.get("rel") or "").lower().split():
            found.append((0, a))
        elif _NEXT_LABEL.match(a.get("aria-label") or "") or _NEXT_LABEL.match(a.get("title") or ""):
            found.append((1, a))
        elif words in _NEXT_WORDS:
            found.append((2, a))
    for _rank, a in sorted(found, key=lambda x: x[0]):
        if selector := selector_for(root, a):
            return selector
    return None


#: Tags a posting's text sits in as a whole: a block that holds other blocks, never a paragraph or a list item in it.
_CONTAINERS = frozenset({"div", "section", "article", "main", "td"})
_BLOCKS = hs.BLOCKS | {"td", "th", "body", "#root"}
#: A run of words shorter than this is a label, a menu entry or a button, not the posting's prose.
PROSE_MIN = 25
#: The share of the page's prose the element chosen must hold: the title line, a sidebar and a notice below the
#: posting are outside it, so it is not all of it.
COVER = 0.9


def _prose(root: hs.Node) -> tuple[dict[int, int], dict[int, int], list[hs.Node]]:
    """The page's prose under every element, as {id(element): characters}, with each element's depth and the elements
    in document order. Prose is the words of a block that make a run of at least `PROSE_MIN` characters; the page's
    chrome (`adapters._common._chrome`: menus, header and footer, asides, forms, dialogs, sharing, cookies) and what a
    visitor never reads (script, style) hold none."""
    runs: dict[int, int] = {}
    depth, order, stack = {id(root): 0}, [root], [root]
    while stack:
        node = stack.pop()
        for child in node.children:
            if isinstance(child, str):
                if words := len(" ".join(child.split())):
                    block = node
                    while block.tag not in _BLOCKS and block.parent is not None:
                        block = block.parent
                    runs[id(block)] = runs.get(id(block), 0) + words
            elif child.tag not in hs.SILENT and not _common._chrome(child.tag, child.attrs, False):
                depth[id(child)] = depth[id(node)] + 1
                order.append(child)
                stack.append(child)
    prose = {id(n): runs.get(id(n), 0) if runs.get(id(n), 0) >= PROSE_MIN else 0 for n in order}
    for node in reversed(order):  # a child is after its parent in `order`
        if node.parent is not None and node is not root:
            prose[id(node.parent)] += prose[id(node)]
    return prose, depth, order


def posting_container(root: hs.Node) -> hs.Node | None:
    """The smallest element that holds the whole of a page's posting: of the blocks that hold at least `COVER` of the
    page's prose, the one with the least of it (the innermost, where wrappers hold the same). Not the first element of
    a usual name, which may be one section of the posting, or an empty one, or another block of the page's. None when
    the page has less prose than a posting (`page_recipe.TEXT_MIN`)."""
    prose, depth, order = _prose(root)
    total = prose[id(root)]
    if total < page_recipe.TEXT_MIN:
        return None
    holding = [n for n in order if n.tag in _CONTAINERS and prose[id(n)] >= COVER * total]
    for node in sorted(holding, key=lambda n: (prose[id(n)], -depth[id(n)])):
        if len(node.text()) >= page_recipe.TEXT_MIN:
            return node
    return None


def container_selector(root: hs.Node, node: hs.Node) -> str | None:
    """A selector whose first match on the page is `node`: a usual place of a description when that is it, else
    `selector_for`."""
    for usual in _DESCRIPTION:
        if _first(root, usual) is node:
            return usual
    return selector_for(root, node)


def text_selector(root: hs.Node) -> str | None:
    """The selector of the element of a posting's page that holds its whole description: its container
    (`posting_container`); for a page that has none, the first of the usual places that holds at least
    `page_recipe.TEXT_MIN` characters."""
    if (node := posting_container(root)) is not None and (selector := container_selector(root, node)):
        return selector
    for selector in _DESCRIPTION:
        node = _first(root, selector)
        if node is not None and len(node.text()) >= page_recipe.TEXT_MIN:
            return selector
    return None


def list_candidates(root: hs.Node, url: str) -> list[dict]:
    """The recipes the start page's HTML makes, the likeliest first: {recipe, rendered (rows it reads on this page),
    sample (links to open to see they are postings)}."""
    out: list[dict] = []
    for group in page_recipe.link_groups(anchors_of(root, url))[:page_recipe.CANDIDATES]:
        recipe: dict = {"kind": "html", "url": url, "links": group["selector"]}
        if group.get("row"):
            recipe["row"] = group["row"]
            recipe.update(row_fields(root, group["selector"], group["row"], url))
        if nxt := next_selector(root, url):
            recipe["next"] = nxt
        try:
            parsed = skills.parse_recipe(recipe)
        except ValueError:
            continue
        assert isinstance(parsed, skills.HtmlRecipe)
        rows = html_recipe.rows_of(root, url, parsed)
        if rows:
            sample = [r["href"] for r in rows][:page_recipe.SAMPLES]
            out.append({"recipe": recipe, "rendered": len(rows), "sample": sample})
    return out


def cards_of(html: str, url: str) -> list[dict]:
    """The roles a rendered results page shows, as {id, title, company} (`same_roles.clean_cards`): the rows of the
    posting-link group `list_candidates` ranks first. Empty when the page shows none. Pure."""
    root = hs.parse(html)
    for cand in list_candidates(root, url)[:1]:
        parsed = skills.parse_recipe(cand["recipe"])
        assert isinstance(parsed, skills.HtmlRecipe)
        return same_roles.clean_cards([{"id": r.get("job_id"), "title": r.get("title"), "company": r.get("company")}
                                       for r in html_recipe.rows_of(root, url, parsed)])
    return []


def pattern_for(sample: str, host: str) -> str:
    """A pattern that matches the addresses of posting pages shaped like `sample` whole: its host, its fixed path words,
    an id (or anything after one) as one path part, and any query."""
    parts = urlsplit(sample)
    names = host[2:] if host.startswith("*.") else None
    head = r"https?://" + (r"[^/]+\." + re.escape(names) if names else re.escape(parts.hostname or host))
    path, after_id = [], False
    room = 2 - (1 if names else 0)  # a recipe's pattern may hold three long repeats (`skills.WIDE_REPEATS_MAX`)
    for part in (p for p in parts.path.split("/") if p):
        if after_id or any(c.isdigit() for c in part):
            if len(path) - fixed(path) >= room:
                break  # a deep address: the rest of it is one tail, whatever its parts
            path.append("[^/]+")
            after_id = True
        else:
            path.append(re.escape(part))
    tail = r"[^#]*" if after_id and len(path) - fixed(path) >= room else r"/?\??[^#]*"
    return head + "".join("/" + p for p in path) + tail


def fixed(path: list[str]) -> int:
    """How many of a pattern's path parts are fixed words, not the `[^/]+` of an id."""
    return sum(1 for p in path if p != "[^/]+")


# --- recording in Chrome ----------------------------------------------------------------------------------------


async def _rendered(t: page_recipe.Tab, url: str) -> tuple[str | None, int | None]:
    """The page's HTML as Chrome rendered it, or the status of a refusal or a bot check."""
    if (status := await t.go(url)) is not None:
        return None, status
    here = await recipe_interact.snapshot(t.tools, t.sid)
    if here is None:
        return None, None
    if recipe_record.looks_walled(here.html):
        return None, recipe_record.BOT_WALL_STATUS
    return here.html, None


async def propose_list(t: page_recipe.Tab, fetch: polite.Fetcher, url: str,
                       ) -> tuple[dict | None, int, str | None, int | None]:
    """The recipe the rendered start page and a few postings opened in the tab make: (recipe, rows the rendered first
    page read, why none, the status of a refusal)."""
    html, status = await _rendered(t, url)
    if status is not None or html is None:
        return None, 0, None if status is not None else "the page could not be read", status
    root = hs.parse(html)
    candidates = list_candidates(root, url)
    if not candidates:
        return None, 0, "no group of posting links could be told apart in the page's HTML", None
    for cand in candidates:
        text: str | None = None
        postings: bool | None = None  # None: no link could be opened, so the group is taken on its shape
        for link in cand["sample"]:
            if not await fetch.allows(link):
                continue
            await bc.sleep(page_recipe.ACTION_GAP)
            opened, status = await _rendered(t, link)
            if status is not None:
                return None, 0, None, status
            if opened is None:
                continue
            if page_recipe.is_hub(link, anchors_of(hs.parse(opened), url)):
                postings = False
                break
            postings = True
            text = text or text_selector(hs.parse(opened))
        if postings is not False:
            recipe = dict(cand["recipe"])
            if text:
                recipe["text"] = text
            return recipe, cand["rendered"], None, None
    return None, 0, ("the links on the page lead to pages that list jobs (a team's, a category's or a place's), "
                     "not to single postings"), None


async def plain_source(fetch: polite.Fetcher, url: str) -> tuple[dict | None, str | None, int | None]:
    """How the page a *plain fetch* gets holds the posting's whole text, checked on that page and not on the rendered
    one: ({"jsonld": True} when it embeds a schema.org JobPosting whose description is whole, else {"text": selector}
    for the element that holds the posting, else None), why the page could not be read, the status of a block. The
    JobPosting comes first: a page that carries one is a script-driven page whose HTML holds the page's furniture, not
    the posting."""
    try:
        html, _raw = await html_recipe.fetch_page(fetch, url)
    except skills.SkillError as exc:
        return None, str(exc), _blocked(exc)
    except Refused as exc:
        return None, str(exc), None
    embedded = html_recipe.jsonld_text(html)
    if embedded is not None and whole.why_not(embedded) is None:
        return {"jsonld": True}, None, None
    root = hs.parse(html)
    node = posting_container(root)
    if node is not None and whole.why_not(node.text()) is None:
        if selector := container_selector(root, node):
            return {"text": selector}, None, None
    return None, None, None


async def propose_posting(t: page_recipe.Tab, fetch: polite.Fetcher, url: str, host: str,
                          ) -> tuple[dict | None, str | None, int | None]:
    """The host recipe the posting page makes: (recipe, why none, the status of a refusal). The text is found in the
    page a plain fetch gets (`plain_source`); only when that holds none is it looked for in the page Chrome rendered,
    for the verification to say what the plain page lacks."""
    html, status = await _rendered(t, url)
    if status is not None or html is None:
        return None, None if status is not None else "the page could not be read", status
    source, why, status = await plain_source(fetch, url)
    if why is not None or status is not None:
        return None, why, status
    if source is None:
        selector = text_selector(hs.parse(html))
        if selector is None:
            return None, ("no element of the page's HTML, and no schema.org JobPosting in it, holds the posting's "
                          "text"), None
        source = {"text": selector}
    recipe = {"kind": "html_posting", "host": host, "url_pattern": pattern_for(url, host), **source}
    try:
        parsed = skills.parse_recipe(recipe)
    except ValueError as exc:
        return None, f"the steps recorded make no valid recipe ({exc})", None
    assert isinstance(parsed, skills.HtmlPostingRecipe)
    if not html_recipe.covers(parsed, url):
        return None, "the address pattern made does not match the page it was made from", None
    return recipe, None, None


# --- verifying on the plain fetch -------------------------------------------------------------------------------------


def _blocked(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    return status if status in recipes.BLOCKED_STATUSES else None


async def _whole_text(fetch: polite.Fetcher, recipe, link: str) -> tuple[bool, str]:
    """Whether the recipe's text element gives a whole posting on `link` by plain fetch, and why not in a clause."""
    got = await html_recipe.read_text(fetch, recipe, link)
    if got is None or got.text is None:
        return False, "the posting's text is not in the page a plain fetch gets"
    if (why := whole.why_not(got.text)) is not None:
        return False, f"the text a plain fetch gets is not a whole posting ({why})"
    return True, ""


async def verify_list(fetch: polite.Fetcher, recipe: dict, rendered: int,
                      ) -> tuple[dict | None, int, str | None, int | None]:
    """Run the list recipe once on the plain fetch: (the recipe, as kept with or without its text, postings read, why
    not, the status of a block). Its text is dropped, and said, when a plain fetch does not give it."""
    parsed = skills.parse_recipe(recipe)
    assert isinstance(parsed, skills.HtmlRecipe)
    try:
        got = await html_recipe.walk(fetch, parsed, enough=page_recipe.VERIFY_ROWS)
        rows, need = len(got.found), max(page_recipe.VERIFY_ROWS, math.ceil(PLAIN_SHARE * rendered))
        if got.first_rows < need or rows < page_recipe.VERIFY_ROWS:
            return None, rows, (
                f"{NEEDS_JS} a plain fetch of the page holds {got.first_rows} postings by these selectors, the "
                f"rendered page showed {rendered}: the page draws them by script, so it is read in the browser"), None
        kept, note = dict(recipe), ""
        if parsed.text:
            ok, why = await _whole_text(fetch, parsed, next(iter(got.found.values()))["href"])
            if not ok:
                kept.pop("text")
                note = f" (the text is left out: {why})"
        return kept, rows, note or None, None
    except skills.SkillError as exc:
        return None, 0, str(exc), _blocked(exc)
    except Refused as exc:
        return None, 0, str(exc), None


def typed_of(rec: SiteRecording) -> tuple[dict, str | None]:
    """What the recording typed, and the search address with blanks it made, from the capture it holds (empty when it
    holds none, or one that cannot be read)."""
    try:
        got = recipes.unpack(rec.body) if rec.body else {}
    except Exception:  # a body that is not a capture: there is no typed search to use
        return {}, None
    got = got if isinstance(got, dict) else {}
    typed = search_fill.clean_typed(got.get("typed")) or {}
    template = got.get("search_url")
    return typed, template if isinstance(template, str) else None


def place_map_of(rec: SiteRecording) -> dict | None:
    """The board's own value for each place the recording picked from its list of suggestions, found by code (the
    search address's `{place}` is filled from it), or None."""
    try:
        got = recipes.unpack(rec.body) if rec.body else {}
    except Exception:
        return None
    return search_fill.clean_place_map(got.get("place_map")) if isinstance(got, dict) else None


def fragments_of(rec: SiteRecording) -> list[dict]:
    """The small HTML pieces the recording's page asked a script endpoint for ({url, body, after}), from the capture it
    holds (none when it holds none, or one that cannot be read)."""
    try:
        got = recipes.unpack(rec.body) if rec.body else {}
    except Exception:
        return []
    found = got.get("fragments") if isinstance(got, dict) else None
    return [f for f in found if isinstance(f, dict) and isinstance(f.get("url"), str) and isinstance(f.get("body"), str)
            ] if isinstance(found, list) else []


def forget_typed(rec: SiteRecording) -> None:
    """Take the owner's typed words out of the recording's capture once the page's HTML is done with it (kept or
    failed), and the pieces whose addresses held them: the rest of the capture stays."""
    if not rec.body:
        return
    try:
        got = recipes.unpack(rec.body)
    except Exception:
        return
    if isinstance(got, dict) and ("typed" in got or "fragments" in got or "place_map" in got):
        got.pop("typed", None)
        got.pop("fragments", None)
        got.pop("place_map", None)
        rec.body = recipes.pack(got)


def refuse_literals(recipe: dict, typed: dict | None) -> str | None:
    """Why a recipe may not be kept with its search address: it holds a word the owner typed (a title or a place),
    whole, in the address's path or query. None when it is clean."""
    for name in ("search_url", "fragment_url"):
        template = recipe.get(name)
        if template and (left := search_fill.literals_left(template, None, None, typed)):
            return (f"the search address holds the words typed in the recording ({', '.join(map(repr, left))}): a "
                    "recipe never stores the owner's titles")
    return None


def _place_value(place_map: dict | None, word: str | None) -> str | int | None:
    """What the search address's `{place}` is filled with for a place: the board's own value for it when the recipe
    has a place map, else the place's own words."""
    if word is None or not place_map:
        return word
    return place_map.get(search_fill.place_key(word))


def with_template(recipe: dict, field: str, template: str, typed: dict, place_map: dict | None = None) -> dict | None:
    """The list recipe with its search address (`field`), and, when the address holds `{since}`, how that is written
    (made by code from what the recording learned of the recency filter); None when the address needs a recency filter
    the recording did not find."""
    candidate = {k: v for k, v in {**recipe, field: template}.items() if k != "place_map"}
    if place_map and "place" in skills.placeholders_of(template):
        candidate["place_map"] = place_map
    if "since" not in skills.placeholders_of(template):
        return {k: v for k, v in candidate.items() if k != "since"}
    spec = search_fill.since_spec(typed)
    return {**candidate, "since": spec} if spec is not None else None


def earlier_url(fragments: list[dict], piece: dict) -> str | None:
    """The address the page asked at just before the recency filter was changed, for the piece that followed the change
    (None for any other piece): the place's, else the second title's, else the first's, at the same endpoint."""
    if piece.get("after") != "since":
        return None
    for tag in (*search_fill.PLACE_TAGS_LAST_FIRST, "second", "phrase"):
        for f in fragments:
            if f.get("after") == tag and urlsplit(f["url"]).path == urlsplit(piece["url"]).path:
                return f["url"]
    return None


async def verify_search(fetch: polite.Fetcher, recipe: dict, typed: dict, template: str | None,
                        field: str = "search_url", place_map: dict | None = None,
                        ) -> tuple[dict, str | None, int | None]:
    """The list recipe with the search address made from the recording (`field`: `search_url`, or `fragment_url` for
    the address of a small piece of HTML), when a plain fetch of that search (the typed title and, when the address has
    a place, the typed place) reads `VERIFY_ROWS` postings by the recipe's selectors: (the recipe as kept, a clause
    saying why the search was left out (None: it was kept or there was none), the status of a block)."""
    if not template or not typed.get("phrase"):
        return recipe, None, None
    candidate = with_template(recipe, field, template, typed, place_map)
    if candidate is None:
        return recipe, " (the search is left out: it needs a recency filter the recording did not find)", None
    if (why := refuse_literals(candidate, typed)) is not None:
        return recipe, f" (the search is left out: {why})", None
    try:
        parsed = skills.parse_recipe(candidate)
    except ValueError as exc:
        return recipe, f" (the search is left out: {exc})", None
    assert isinstance(parsed, skills.HtmlRecipe)
    one = search_fill.Search((typed["phrase"],), (typed["place"],) if typed.get("place") else ())
    try:
        got = await html_recipe.walk(fetch, parsed, enough=page_recipe.VERIFY_ROWS, search=one)
    except skills.SkillError as exc:
        status = _blocked(exc)
        return (recipe, None, status) if status is not None else (
            recipe, f" (the search is left out: {exc})", None)
    except Refused as exc:
        return recipe, f" (the search is left out: {exc})", None
    if len(got.found) < page_recipe.VERIFY_ROWS:
        return recipe, f" (the search is left out: a plain fetch of it reads {len(got.found)} postings)", None
    return candidate, None, None


class _Recorded:
    """A fetcher that answers one address with the piece the recording holds and every other with 404: what the offline
    proof of a fragment runs the recipe on."""

    deadline = None

    def __init__(self, address: str, body: str) -> None:
        self.address, self.body, self.requests = address, body, 0

    async def allows(self, _url: str) -> bool:
        return True

    async def get(self, url: str, **_kw) -> httpx.Response:
        self.requests += 1
        if recipes._norm_url(url) == recipes._norm_url(self.address):
            return httpx.Response(200, text=self.body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="", headers={"content-type": "text/html"})


def _fragment_order(fragments: list[dict]) -> list[dict]:
    """The recorded pieces, the one the page asked for after the place first, then after the second title, then after
    the first (the page's last state says the most about its search)."""
    rank = {"since": -1, **{tag: 0 for tag in search_fill.PLACE_TAGS_LAST_FIRST}, "second": 1, "phrase": 2}
    return sorted(fragments, key=lambda f: rank.get(str(f.get("after")), 3))


def fragment_problems(recipe: dict, typed: dict | None, fragments: list[dict]) -> tuple[list[str], int]:
    """What is wrong with a recipe's `fragment_url` against the recording, and how many postings the recipe reads from
    the recorded piece (the offline proof, as `recipes.verify_offline` is for a request): a blank with no word typed for
    it, a typed word still whole in the address, no recorded piece the address filled with the typed words is the
    address of (so a blank sits where the words were not), and a piece the selectors read too few postings from.
    ([], rows) when it holds."""
    try:
        parsed = skills.parse_recipe(recipe)
    except ValueError as exc:
        return [f"the recipe is not valid: {exc}"], 0
    if not isinstance(parsed, skills.HtmlRecipe) or not parsed.fragment_url:
        return ["the recipe has no fragment_url"], 0
    template, typed = parsed.fragment_url, typed or {}
    held = skills.placeholders_of(template)
    problems = [f"{{{name}}} is used, but nothing was typed for it in the recording"
                for name in ("phrase", "place") if name in held and not typed.get(name)]
    if "since" in held and search_fill.since_spec(typed) is None:
        problems.append("{since} is used, but the recording found no recency filter")
    problems += [f"the word typed in the recording ({w!r}) is still in the address: use {{phrase}} or {{place}}"
                 for w in search_fill.literals_left(template, None, None, typed)]
    if problems:
        return problems, 0
    place = _place_value(parsed.place_map, typed.get("place")) if "place" in held else None
    since = typed["since"]["after"] if "since" in held else None
    for piece in _fragment_order(fragments):
        for phrase in dict.fromkeys(w for w in (typed.get("second"), typed.get("phrase")) if w):
            if recipes._norm_url(skills.search_address(template, phrase, place, since=since)) != recipes._norm_url(
                    piece["url"]):
                continue
            recorded = hs.parse(piece["body"])
            rows = len(html_recipe.rows_of(recorded, piece["url"], parsed))
            if rows < page_recipe.VERIFY_ROWS:
                return [f"the recipe's selectors read {rows} postings from the recorded piece"], rows
            return [], rows
    return ["the recording shows no piece of HTML asked for at this address filled with the typed words: every blank "
            "must sit where the typed words were"], 0


async def replay_fragment(recipe: dict, typed: dict, fragments: list[dict]) -> int:
    """How many postings the recipe reads when its fragment address is filled with the typed words and answered by
    the recorded piece alone (no network): the proof the filled address is the one the page asked at."""
    parsed = skills.parse_recipe(recipe)
    assert isinstance(parsed, skills.HtmlRecipe) and parsed.fragment_url
    held = skills.placeholders_of(parsed.fragment_url)
    word = typed.get("place") if "place" in held else None
    place = _place_value(parsed.place_map, word)
    since = typed["since"]["after"] if "since" in held else None
    for piece in _fragment_order(fragments):
        for phrase in dict.fromkeys(w for w in (typed.get("second"), typed.get("phrase")) if w):
            if recipes._norm_url(skills.search_address(parsed.fragment_url, phrase, place, since=since)) == (
                    recipes._norm_url(piece["url"])):
                got = await html_recipe.walk(cast(Any, _Recorded(piece["url"], piece["body"])), parsed, pages=1,
                                             search=search_fill.Search((phrase,), (word,) if word else (),
                                                                       since_literal=since))
                return len(got.found)
    return 0


def card_recipes(fragments: list[dict], url: str) -> list[tuple[dict, dict]]:
    """The recipes the recorded pieces' own cards make, the likeliest first, each with its piece: for a board whose
    start page holds no cards a plain fetch gets."""
    out: list[tuple[dict, dict]] = []
    for piece in _fragment_order(fragments):
        for cand in list_candidates(hs.parse(piece["body"]), url)[:1]:
            out.append((cand["recipe"], piece))
    return out


async def attach_fragment(fetch: polite.Fetcher, recipe: dict, typed: dict, fragments: list[dict],
                          ) -> tuple[dict | None, int, str | None, int | None]:
    """The list recipe with the fragment address the recording's pieces make, when both proofs hold (`fragment_problems`
    offline, the replay with the filled address, then a plain fetch of the live search, `verify_search`): (the recipe
    as kept with `fragment_url` and, when the live read finds one, its text element left or dropped; postings the plain
    fetch read; a clause saying why not; the status of a block). The recipe is None when none was made."""
    if not typed.get("phrase") or not fragments:
        return None, 0, None, None
    why: str | None = None
    ids = recipes.html_place_ids(fragments, typed)
    for piece in _fragment_order(fragments):
        mine = ids if ids is not None and urlsplit(piece["url"]).path == ids[0] and ids[1][0] == "q" else None
        key = mine[1][1] if mine is not None else None
        template = recipe_record.fragment_template(piece["url"], typed, earlier_url(fragments, piece), key)
        if template is None:
            continue
        candidate = with_template(recipe, "fragment_url", template, typed, mine[2] if mine is not None else None)
        if candidate is None:
            why = "it needs a recency filter the recording did not find"
            continue
        problems, _rows = fragment_problems(candidate, typed, fragments)
        if problems:
            why = "; ".join(problems)
            continue
        if await replay_fragment(candidate, typed, fragments) < page_recipe.VERIFY_ROWS:
            why = "the recipe filled with the typed words did not read the recorded piece"
            continue
        kept, left_out, status = await verify_search(fetch, recipe, typed, template, "fragment_url",
                                                     candidate.get("place_map"))
        if status is not None:
            return None, 0, None, status
        if kept.get("fragment_url") != template:
            why = (left_out or "").strip(" ()") or "a plain fetch of the search read no postings"
            continue
        parsed = skills.parse_recipe(kept)
        assert isinstance(parsed, skills.HtmlRecipe)
        one = search_fill.Search((typed["phrase"],), (typed["place"],) if typed.get("place") else ())
        rows = page_recipe.VERIFY_ROWS
        try:
            got = await html_recipe.walk(fetch, parsed, enough=page_recipe.VERIFY_ROWS, pages=1, search=one)
            rows = len(got.found)
            first = next(iter(got.found.values()))["href"]
            if kept.get("text"):
                ok, _why = await _whole_text(fetch, parsed, first)
                if not ok:
                    kept = {k: v for k, v in kept.items() if k != "text"}
            else:
                source, _why, _status = await plain_source(fetch, first)
                if source and "text" in source:
                    kept = {**kept, "text": source["text"]}
        except (skills.SkillError, Refused, StopIteration):
            pass  # the list read; the text is only an extra
        return kept, rows, None, None
    return None, 0, why, None


async def verify_posting(fetch: polite.Fetcher, recipe: dict, url: str) -> tuple[bool, str | None, int | None]:
    """The host recipe's text element on the recorded page by plain fetch: (ok, why not, the status of a block)."""
    parsed = skills.parse_recipe(recipe)
    assert isinstance(parsed, skills.HtmlPostingRecipe)
    try:
        ok, why = await _whole_text(fetch, parsed, url)
    except skills.SkillError as exc:
        return False, str(exc), _blocked(exc)
    except Refused as exc:
        return False, str(exc), None
    return ok, None if ok else f"{NEEDS_JS} {why}: the page draws it by script, so it is read in the browser", None


# --- keeping ----------------------------------------------------------------------------------------------------


async def keep(session: AsyncSession, owner: str, rec: SiteRecording, recipe: dict, rows: int, now: datetime,
               ) -> SiteRecipe:
    """The verified recipe as the next version of the site's (or the host's) recipes, live; the one before it is
    retired. A site that is off and that nobody has decided about is switched on by it (`where.runbook_saved`)."""
    recipe = {**recipe, "provenance": {"recorded_at": now.isoformat(), "recording_sha256": "", "model": None,
                                       "prompt_version": PROMPT_VERSION, "tested_on": rows}}
    parsed = skills.parse_recipe(recipe)
    typed = typed_of(rec)[0]
    if (problem := refuse_literals(recipe, typed)) is not None:
        raise ValueError(problem)
    if (rec.site_id is not None and isinstance(parsed, skills.HtmlRecipe) and (
            note := recipe_record.place_note(typed, skills.placeholders_of(parsed.template() or "")))):
        site = await session.get(Site, rec.site_id)
        if site is not None and not site.read_note:  # said on the board's status, in one line
            site.read_note = note
    of = (SiteRecipe.host == rec.host) if rec.site_id is None else (SiteRecipe.site_id == rec.site_id)
    last = await session.scalar(select(func.max(SiteRecipe.version)).where(of))
    for old in (await session.scalars(select(SiteRecipe).where(of, SiteRecipe.retired_at.is_(None)))).all():
        old.state, old.retired_at = "retired", now
    forget_typed(rec)
    row = SiteRecipe(owner=owner, site_id=rec.site_id, host=rec.host, version=(last or 0) + 1,
                     recipe=parsed.model_dump(mode="json", by_alias=True), recording_id=rec.id, state="live",
                     created_at=now, bad_nights=0, health=[])
    session.add(row)
    rec.state, rec.recorded_at, rec.note = "written", now, None
    await session.flush()
    await where.runbook_saved(session, rec.site_id)
    if rec.site_id is not None:
        from app.jobsearch.browser.recipes import live as recipe_live  # it uses this module's neighbours

        await recipe_live.after_kept(session, owner, rec.site_id)
    return row


async def record_by_code(owner: str, rec_id) -> dict:
    """The rest of a `record` item whose page made no data request a recipe could repeat: record selectors from the
    rendered page, run the recipe once on a plain fetch, keep it when it reads. Returns a hand-back's fields (`finished`
    or `blocked`, a note). Raises `browser_code.BrowserUnavailable` when the app's Chrome is not there."""
    from app.jobsearch.browser import pace as browser_pace

    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        if rec is None or rec.state != "requested":
            return {"result": browser_pace.FINISHED, "note": "the request was answered already"}
        site = await session.get(Site, rec.site_id) if rec.site_id else None
        url, host = (site.url if site is not None else rec.url) or "", rec.host or ""
        for_host = rec.site_id is None
        typed, template = typed_of(rec) if site is not None and added.owner_added_search(site) else ({}, None)
        place_map = place_map_of(rec) if typed else None
        fragments = fragments_of(rec) if typed else []
    if not public_url(url) or (for_host and not host):
        return await _failed(rec_id, "there is no public address to read")
    recipe: dict | None = None
    rows, rendered, why, status, note = 0, 0, None, None, None
    fetch = polite.make()
    try:
        if not await fetch.allows(url):
            return await _failed(rec_id, "robots.txt asks not to read the page")
        async with page_recipe._one_tab, page_recipe.tab() as t:
            if for_host:
                recipe, why, status = await propose_posting(t, fetch, url, host)
            else:
                recipe, rendered, why, status = await propose_list(t, fetch, url)
        proposed = recipe
        if recipe is not None and status is None:
            if for_host:
                ok, why, status = await verify_posting(fetch, recipe, url)
                rows = 1 if ok else 0
                recipe = recipe if ok else None
            else:
                recipe, rows, note, status = await verify_list(fetch, recipe, rendered)
                why = note if recipe is None else None
                note = note if recipe is not None else None
                if recipe is not None and status is None:
                    recipe, left_out, status = await verify_search(fetch, recipe, typed, template,
                                                                   place_map=place_map)
                    note = (note or "") + (left_out or "") or None
                    if recipe is not None and not recipe.get("search_url") and fragments:
                        got, got_rows, says, status = await attach_fragment(fetch, recipe, typed, fragments)
                        recipe = got or recipe
                        rows = got_rows if got else rows
                        note = (note or "") + (f" (the fragment search is left out: {says})" if says else "") or None
        if not for_host and recipe is None and status is None and fragments:
            # The plain start page holds no cards (it draws them by script), but the page asked a script endpoint for
            # a piece of HTML that does: the selectors that read the rendered page, or the piece's own cards, read it.
            starts = [proposed] if proposed is not None else []
            starts += [r for r, _piece in card_recipes(fragments, url)]
            for start in starts:
                got, got_rows, says, status = await attach_fragment(fetch, {**start, "url": url}, typed, fragments)
                if status is not None:
                    break
                if got is not None:
                    recipe, rows, why, note = got, got_rows, None, None
                    break
    finally:
        await fetch.close()
    if status is not None:
        return {"result": browser_pace.BLOCKED, "url": url,
                "note": f"the page answered {status} or showed a bot check: a block, so the site rests"}
    if recipe is None:
        return await _failed(rec_id, why or "no selectors could be recorded")
    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        if rec is None or rec.state != "requested":
            return {"result": browser_pace.FINISHED, "note": "the request was answered already"}
        await keep(session, owner, rec, recipe, rows, datetime.now(UTC))
        await session.commit()
    return {"result": browser_pace.FINISHED,
            "note": f"an HTML recipe was recorded and read {rows} postings from the page itself{note or ''}"}


async def _chain(session: AsyncSession, rec: SiteRecording) -> bool:
    """The one link after a page that needs JavaScript, for a site the owner added: a recording in the app's own Chrome
    (`recipe_live.CHAIN`, taken at any hour), at most once for a site, and only after the first recording or the
    first read's demotion, never after one the owner asked for by name (`page`), a repair, or the chain's own link.
    True when it was asked for."""
    from app.jobsearch.browser.recipes import live as recipe_live  # it uses this module's neighbours

    if rec.site_id is None or rec.reason not in ("first", recipe_live.DEMOTE):
        return False
    site = await session.get(Site, rec.site_id)
    if site is None or not added.owner_added_search(site):
        return False
    if await session.scalar(select(SiteRecording.id).where(
            SiteRecording.site_id == site.id, SiteRecording.reason == recipe_live.CHAIN).limit(1)) is not None:
        return False
    await recipe_record.request(session, rec.owner, site, recipe_live.CHAIN)
    return True


async def _failed(rec_id, why: str) -> dict:
    from app.jobsearch.browser import pace as browser_pace

    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        if rec is None or rec.state != "requested":
            return {"result": browser_pace.FINISHED, "note": "the request was answered already"}
        if rec.body is not None and rec.note:  # the data writer found none first: say both
            why = f"{why} ({rec.note})"
        rec.state, rec.note = "failed", why[:300]
        forget_typed(rec)
        if why.startswith(NEEDS_JS) and await _chain(session, rec):  # the page draws itself: try it in Chrome, once
            why = f"{why[:240]} (it is tried in the app's browser next)"
        await session.commit()
    return {"result": browser_pace.FINISHED, "note": why[:300]}
