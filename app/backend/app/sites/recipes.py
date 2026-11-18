"""Recording a site's job listings and checking a recipe against the recording. Pure of the network: a recording is data
the app's own Chrome made (`jobsearch/browser/recipes/record.py`), and everything here works on it.

**Capture.** What one load of a site's listing page asked for and was answered: for each response the method, address,
body sent, status, content type and body. `sanitize` keeps only what a recipe could replay: GET and POST answered 200
with JSON, on public addresses, at most `RESPONSES_MAX` of them, each at most `BODY_MAX` bytes. Request headers and
cookies are never part of a capture (the browser layer does not pass them on); a response whose address or body sent
carries a token, key or session (`security.find_secret`, or a parameter named like one) is dropped, because a recipe
that needs one is refused anyway. The capture's hash (`fingerprint`) is what a recipe's provenance names.

**What the model sees.** `trim` makes the capture short and safe to read as data: for each response its request and the
shape of the answer, with the longest lists in it and their first items cut to a few hundred characters. Never
instructions: the writer's instructions say so.

**Replay.** `ReplayFetcher` stands where the polite fetcher stands and answers each request from the capture, so the
one executor (`skills.run_recipe`) runs the recipe with no network. A request the capture has no answer for gets the
nearest recorded answer for the same endpoint with its list emptied (the pages the page never loaded: the read then
ends there); an endpoint never recorded is an error. `verify_offline` runs a recipe so and says whether it reproduced
the recorded list: rows found against rows recorded, required fields filled in 90 percent or more, dates that parse,
pay that is plausible, public addresses, and a detail request only if the capture shows one.

**Blanks.** A recording of a site may say what was typed in its search (`typed`: the owner's first title, a second one
and a place) and mark each data request with the typing it followed (`after`). `verify_offline` then holds a recipe that
writes `{phrase}` or `{place}` to four things (`sites/search_fill.py` says where the words go): every blank has a word
that was typed; the blank sits in the field the two searches differ in; none of the typed words remains whole in the
recipe's address, params or body; and the recipe filled with the typed words makes exactly the request the page made, so
a blank in the wrong place cannot pass on a request that is merely close. That proves the blanks sit where the words
were, not that the site honours other words: the first night's read with the owner's titles is what judges that
(`judge_night`).

**Health.** `judge_night` says whether a night's read looks broken (an error, no rows, rows far under the recent
median, fields no longer filled in). A block is not breakage: it is a stop, and cools the site."""

import hashlib
import json
import re
import statistics
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

import httpx

from app.agents.security import find_secret
from app.jobsearch.fetch.polite import Fetcher, public_url
from app.jobsearch.postings import whole
from app.sites import embedded, html_select, same_roles, search_fill, skills

RESPONSES_MAX = 40
#: A job list with every description in it is megabytes (a board's whole list in one answer): each answer may be this
#: big, and a capture this big in all.
BODY_MAX = 6_000_000
CAPTURE_MAX = 16_000_000
CAPTURE_VERSION = 1
#: A script call's answer that is a small piece of HTML holding job cards (a fragment), kept beside the data
#: responses: at most `FRAGMENTS_MAX` of them, each at most `FRAGMENT_MAX` characters (a card list, never a page).
FRAGMENT_MAX = 300_000
FRAGMENTS_MAX = 6
#: A parameter or key named like a credential.
_SECRET_NAME = re.compile(
    r"token|secret|session|passw|auth|cookie|csrf|xsrf|jwt|signature|api[-_]?key|bearer|^sig$|^key$", re.I)
#: What the model is given of a capture, in characters, and of each text in an item.
TRIM_MAX = 60_000
#: The most of a request's own body shown to the writer (a search payload is far less; the recording keeps it whole).
POST_SHOWN = 6_000
ITEM_TEXT_MAX = 240
#: Required fields filled in at least this share; rows reproduced at least this share of those recorded.
FILL_MIN = 0.9
#: A data request's answer is the site's listing when it holds a list of at least this many objects.
LISTING_MIN = 3
#: Pay that is plausible as a yearly figure.
PAY_RANGE = (10_000, 3_000_000)
#: Nights of rows kept as the recent median, and how far under it a night may fall before it looks broken.
HISTORY = 14
DROP_BELOW = 0.4
MEDIAN_NEEDS = 3
#: Nights a recipe may fail in a row before the site is marked stale and a re-record is queued; rewrites that may fail
#: before the owner is told; days between two rewrites of one site.
STALE_AFTER = 2
REWRITES_MAX = 3
REWRITE_EVERY_DAYS = 7


class NotRecorded(skills.SkillError):
    """The recipe asked for an address the recording never made."""


# --- capture ----------------------------------------------------------------------------------------------------------


def _secret_query(url: str) -> bool:
    return any(_SECRET_NAME.search(k) or find_secret(v) for k, v in parse_qsl(urlsplit(url).query))


def _secret_body(post_data: str | None) -> bool:
    if not post_data:
        return False
    try:
        found = json.loads(post_data)
    except ValueError:
        return bool(find_secret(post_data))

    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            return any(_SECRET_NAME.search(str(k)) or walk(v) for k, v in value.items())
        if isinstance(value, list):
            return any(walk(v) for v in value)
        return isinstance(value, str) and bool(find_secret(value))

    return walk(found)


def is_fragment(body: Any) -> bool:
    """Whether an answer is a small piece of HTML that holds repeated job cards: markup (it starts with a tag) of at
    most `FRAGMENT_MAX` characters in which at least `LISTING_MIN` links with words of their own sit in elements of
    one shape (the same tag and classes). Pure: the markup is parsed as data and nothing in it is run."""
    if not isinstance(body, str) or len(body) > FRAGMENT_MAX or not body.lstrip().startswith("<"):
        return False
    shapes: dict[tuple, int] = {}
    for a in html_select.select(html_select.parse(body), "a"):
        if not (a.get("href") or "").strip() or len(a.text().strip()) < 4:
            continue
        card = a.parent
        while card is not None and card.tag not in ("li", "tr", "article", "div", "section", "#root"):
            card = card.parent
        if card is None or card.tag == "#root":
            continue
        shape = (card.tag, tuple(sorted(card.classes())))
        shapes[shape] = shapes.get(shape, 0) + 1
    return bool(shapes) and max(shapes.values()) >= LISTING_MIN


def sanitize(raw: Any, page_url: str, recorded_at: datetime | None = None) -> dict:
    """The capture a recipe may be made from, out of what the browser layer reported (`raw`: a list of
    {method, url, post_data, status, content_type, body}). Returns {version, page_url, recorded_at, responses, dropped}
    where `dropped` counts what was left out and why."""
    dropped: dict[str, int] = {}
    out: list[dict] = []
    seen: set[tuple] = set()
    blobs: list[dict] = []
    fragments: list[dict] = []
    typed: dict | None = None
    place_map: dict | None = None
    cards: list[dict] = []

    def drop(why: str) -> None:
        dropped[why] = dropped.get(why, 0) + 1

    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        if "blob" in r:  # a data block the page's HTML holds (`sites/embedded.py`), not a request
            blobs.append(r)
            continue
        if "typed" in r and "method" not in r:  # what the visitor typed in the search (`sites/search_fill.py`)
            typed = search_fill.clean_typed(r["typed"])
            continue
        if "place_map" in r and "method" not in r:  # the board's own value for each place, found by code
            place_map = search_fill.clean_place_map(r["place_map"])
            continue
        if "cards" in r and "method" not in r:  # the roles the page showed, to compare the recipe's with
            cards = same_roles.clean_cards(r["cards"])
            continue
        after = r.get("after") if r.get("after") in search_fill.AFTER else None
        method, url, body = str(r.get("method") or "").upper(), str(r.get("url") or ""), r.get("body")
        ctype = str(r.get("content_type") or "").lower()
        if method not in ("GET", "POST"):
            drop("not a GET or POST")
        elif not public_url(url):
            drop("not a public address")
        elif int(r.get("status") or 0) != 200 or not isinstance(body, str):
            drop("not a JSON answer")
        elif len(body) > BODY_MAX:
            drop("too big")
        elif _secret_query(url) or _secret_body(r.get("post_data")):
            drop("carries a token, key or session")
        else:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if parsed is None and method == "GET" and is_fragment(body):  # a small piece of HTML holding job cards
                key = (url, after)
                if key in seen:
                    continue
                seen.add(key)
                if len(fragments) >= FRAGMENTS_MAX:
                    drop("over the limit")
                else:
                    fragments.append({"url": url, "status": 200, "content_type": ctype, "body": body,
                                      **({"after": after} if after else {})})
                continue
            if not isinstance(parsed, dict | list):  # data of any type is kept when it is JSON (GraphQL, text/plain)
                drop("not a JSON answer")
                continue
            key = (method, url, r.get("post_data") or "", after)
            if key in seen:
                continue
            seen.add(key)
            out.append({"method": method, "url": url, "post_data": r.get("post_data") or None, "status": 200,
                        "content_type": ctype, "body": body, **({"after": after} if after else {})})
    if len(out) > RESPONSES_MAX:
        drop("over the limit")  # the biggest answers are kept (a job list is one), in the order they were made
        keep = set(sorted(range(len(out)), key=lambda i: -len(out[i]["body"]))[:RESPONSES_MAX])
        out = [r for i, r in enumerate(out) if i in keep]
    total = 0
    for i, r in enumerate(out):  # the biggest answers last: what is over the capture's size in all is left out
        total += len(r["body"])
        if total > CAPTURE_MAX:
            drop("over the size of a capture")
            out = out[:i]
            break
    capture = {"version": CAPTURE_VERSION, "page_url": page_url, "responses": out, "dropped": dropped,
               "recorded_at": (recorded_at or datetime.now(UTC)).isoformat()}
    if typed is not None:
        capture["typed"] = typed
    if fragments:
        capture["fragments"] = fragments
    if place_map:
        capture["place_map"] = place_map
    if cards:
        capture["cards"] = cards
    if blobs and not has_listing(out):  # the fallback: only when no data request carried the listing
        capture["embedded"] = _embedded(blobs, total, drop)
    return capture


def _postings_like(items: list) -> bool:
    """Whether a list of objects looks like job postings: most of its items have a title and an id, address
    or path at the top (a list of video clips or menu entries has not)."""
    def has(item: dict, pattern: str) -> bool:
        return any(re.search(pattern, str(k), re.I) for k in item)
    fits = sum(1 for i in items if has(i, r"title|job_?name|position|role") and has(i, r"id$|url|path|slug|link|req"))
    return fits >= max(2, 2 * len(items) / 3)


def has_listing(responses: list[dict]) -> bool:
    """Whether a data request's answer holds the site's postings (a list of at least `LISTING_MIN` objects that look
    like postings)."""
    for r in responses:
        try:
            if any(len(v) >= LISTING_MIN and _postings_like(v) for _, v in lists_in(json.loads(r["body"]))):
                return True
        except ValueError:
            continue
    return False


def _embedded(blobs: list[dict], used: int, drop: Any) -> list[dict]:
    """The data blocks a recording may keep: on a public address, structured, no token, within the capture's size."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for b in blobs:
        url, name, body = str(b.get("url") or ""), str(b.get("blob") or ""), b.get("body")
        if not embedded.BLOB_NAME.fullmatch(name) or not isinstance(body, str):
            drop("not a data block")
        elif not public_url(url):
            drop("not a public address")
        elif len(body) > BODY_MAX or used + len(body) > CAPTURE_MAX:
            drop("too big")
        elif find_secret(body):
            drop("carries a token, key or session")
        else:
            try:
                structured = embedded.holds_list(json.loads(body))
            except ValueError:
                structured = False
            if not structured:
                drop("not structured data")
            elif (url, name, body) not in seen and len(out) < RESPONSES_MAX:
                seen.add((url, name, body))
                used += len(body)
                out.append({"url": url, "blob": name, "body": body})
    return out


def fingerprint(capture: dict) -> str:
    return hashlib.sha256(json.dumps(capture, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def pack(capture: dict) -> bytes:
    return zlib.compress(json.dumps(capture, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def unpack(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob))


# --- what the model sees ----------------------------------------------------------------------------------------------


def _cut(value: Any, depth: int = 0) -> Any:
    """A value with long texts cut and deep or long parts shortened, so an item shows its shape."""
    if isinstance(value, str):
        return value if len(value) <= ITEM_TEXT_MAX else value[:ITEM_TEXT_MAX] + f"...[{len(value)} characters]"
    if isinstance(value, list):
        return [_cut(v, depth + 1) for v in value[:3]] + ([f"...[{len(value)} items]"] if len(value) > 3 else [])
    if isinstance(value, dict):
        if depth > 5:
            return "{...}"
        return {k: _cut(v, depth + 1) for k, v in list(value.items())[:40]}
    return value


def lists_in(value: Any, path: str = "") -> list[tuple[str, list]]:
    """Every list of objects in an answer with its dotted path, longest first."""
    found: list[tuple[str, list]] = []
    if isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            found.append((path, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            found += lists_in(v, f"{path}.{k}" if path else str(k))
    return sorted(found, key=lambda f: -len(f[1]))


def trim(capture: dict, limit: int = TRIM_MAX) -> str:
    """The capture as short text for a session: each response's request and the lists in its answer (path, how many,
    the first two items cut). Responses with the longest list come first; what does not fit is left out and said."""
    shown: list[tuple[int, dict]] = []
    for r in capture.get("responses", []):
        answer = json.loads(r["body"])
        lists = lists_in(answer)
        keys = list(answer)[:30] if isinstance(answer, dict) else None
        shown.append((len(lists[0][1]) if lists else 0, {
            "request": f"{r['method']} {r['url']}"
                       + (f"  body: {r['post_data'][:POST_SHOWN]}" if r.get("post_data") else ""),
            **({"search_typed": r["after"]} if r.get("after") else {}),
            "answer_keys": keys,
            "lists": [{"path": p or "(the answer itself)", "count": len(v), "first_items": _cut(v[:2])}
                      for p, v in lists[:3]]}))
    for b in capture.get("embedded", []):
        lists = lists_in(json.loads(b["body"]))
        shown.append((len(lists[0][1]) if lists else 0, {
            "embedded_in_page": b["url"], "blob": b["blob"],
            "lists": [{"path": p or "(the block itself)", "count": len(v), "first_items": _cut(v[:2])}
                      for p, v in lists[:3]]}))
    head = {"page": capture.get("page_url"), "responses_kept": len(shown), "left_out": capture.get("dropped", {})}
    text, left = json.dumps(head), 0
    if capture.get("typed"):  # its own line, so `strip_typed` can take it out when the recording is done with
        text += "\n" + json.dumps({"typed": capture["typed"]})
    for _, entry in sorted(shown, key=lambda s: -s[0]):
        piece = json.dumps(entry)
        if len(text) + len(piece) > limit:
            left += 1
            continue
        text += "\n" + piece
    return text + (f"\n({left} more responses left out for length)" if left else "")


def strip_typed(text: str | None) -> str | None:
    """The trimmed capture without its line of typed words (`trim`): what a parse job keeps once its recipe is kept or
    has failed, so the owner's titles are not left in the job's text."""
    if text is None:
        return None
    return "\n".join(line for line in text.split("\n") if not line.startswith('{"typed": '))


# --- replay -----------------------------------------------------------------------------------------------------------


def _norm_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(sorted(parse_qsl(p.query, keep_blank_values=True))), ""))


def _norm_body(post_data: str | None) -> str:
    if not post_data:
        return ""
    try:
        return json.dumps(json.loads(post_data), sort_keys=True)
    except ValueError:
        return post_data


def _bare(value: Any) -> bool:
    """A value that narrows nothing: absent, empty, zero, false, or a first page's number."""
    return value in (None, "", 0, 1, False, "0", "1", "false") or value == {} or value == []


def _compatible(want: dict, have: dict) -> tuple[int, bool]:
    """How many entries a request shares with a recorded one, and whether they differ only in ways a recipe may:
    every entry equal, or bare on either side (a filter left out, a first page's number)."""
    shared, fits = 0, True
    for k in want.keys() | have.keys():
        a, b = want.get(k), have.get(k)
        if a == b:
            shared += 1
        elif not (_bare(a) or _bare(b)):
            fits = False
    return shared, fits


def _empty_lists(value: Any) -> Any:
    if isinstance(value, list):
        return [] if value and all(isinstance(v, dict) for v in value) else value
    if isinstance(value, dict):
        return {k: _empty_lists(v) for k, v in value.items()}
    return value


class ReplayFetcher:
    """Answers a recipe's requests from a capture; nothing is sent. `requests` counts them; `unrecorded` the ones
    answered with an emptied list."""

    deadline = None

    def __init__(self, capture: dict, first_exact: bool = False) -> None:
        #: The recipe's first request must be one the page made, to the letter (a recipe with search blanks); the
        #: pages after it are answered as before.
        self.first_exact = first_exact
        #: The recorded answer the first request was given, when it was an exact one.
        self.hit: dict | None = None
        self.responses = capture.get("responses", [])
        #: A recorded data block is served as the smallest page that holds it.
        self.pages = [{"method": "GET", "url": b["url"], "post_data": None, "content_type": "text/html",
                       "body": embedded.wrap(b["blob"], b["body"]), "blob": b["blob"], "json": b["body"]}
                      for b in capture.get("embedded", [])]
        self.exact = {(r["method"], _norm_url(r["url"]), _norm_body(r.get("post_data"))): r
                      for r in [*self.responses, *self.pages]}
        self.requests = 0
        self.unrecorded = 0
        self.asked: list[str] = []

    def _answer(self, r: dict, body: str, url: str, method: str) -> httpx.Response:
        return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": r["content_type"]},
                              request=httpx.Request(method, url))

    def _closest(self, method: str, url: str, body: str, strict: bool) -> dict | None:
        """The recorded request of the same endpoint closest to this one. `strict`: only one that differs in ways a
        recipe's first request may (it leaves out what the page's own request narrowed by, a filter the visitor
        applied, and sends a first page's number where the page was on another: see `_compatible`)."""
        want_query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        try:
            want_body = json.loads(body) if body else {}
        except ValueError:
            want_body = None
        best, best_score = None, -1
        for r in [*self.responses, *self.pages]:
            if r["method"] != method or urlsplit(r["url"]).path != urlsplit(url).path:
                continue
            try:
                have_body = json.loads(r["post_data"]) if r.get("post_data") else {}
            except ValueError:
                have_body = None
            if not isinstance(want_body, dict) or not isinstance(have_body, dict):
                if _norm_body(r.get("post_data")) != body:
                    continue
                want_body = have_body = {}
            have_query = dict(parse_qsl(urlsplit(r["url"]).query, keep_blank_values=True))
            parts = [_compatible(want_query, have_query), _compatible(want_body, have_body)]
            if strict and not all(fits for _, fits in parts):
                continue
            score = sum(n for n, _ in parts)
            if score > best_score:
                best, best_score = r, score
        return best

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests += 1
        self.asked.append(f"{method} {url}")
        sent = kwargs.get("json")
        key = (method, _norm_url(url), _norm_body(json.dumps(sent) if sent is not None else None))
        if (r := self.exact.get(key)) is not None:
            if self.requests == 1:
                self.hit = r
            return self._answer(r, r["body"], url, method)
        if self.first_exact and self.requests == 1:
            raise NotRecorded(f"the page never made {method} {urlsplit(url).netloc}{urlsplit(url).path} with the "
                              "typed words where the blanks are")
        path = urlsplit(url).path
        if self.requests == 1 and (first := self._closest(method, url, key[2], True)) is not None:
            return self._answer(first, first["body"], url, method)
        near = self._closest(method, url, key[2], False)
        if near is None:
            raise NotRecorded(f"the recording has no answer for {method} {urlsplit(url).netloc}{path}")
        self.unrecorded += 1
        if "blob" in near:
            return self._answer(near, embedded.wrap(near["blob"], json.dumps(
                _empty_lists(json.loads(near["json"])))), url, method)
        return self._answer(near, json.dumps(_empty_lists(json.loads(near["body"]))), url, method)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)


def _detail_pattern(template: str) -> re.Pattern:
    # The marker is plain letters: `_norm_url` re-encodes the query, which would turn a control character in it into
    # `%00` and leave the pattern unable to match an id carried in the query (a `finder=ById;Id="{ext_id}"` query).
    marker = "EXTIDMARKER"
    return re.compile(re.escape(_norm_url(template.replace("{ext_id}", marker))).replace(marker, ".+"))


@dataclass
class Verdict:
    """Whether a recipe reproduced a recording."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    rows: int = 0
    recorded: int = 0
    stats: dict = field(default_factory=dict)


def _recorded_rows(recipe: skills.RequestRecipe, capture: dict) -> int:
    """The distinct rows the capture holds at the recipe's list path, in the answers to its endpoint."""
    path = urlsplit(_norm_url(recipe.url)).path
    rows: set[str] = set()
    if recipe.kind == "embedded":
        for b in capture.get("embedded", []):
            if urlsplit(b["url"]).path == path and b["blob"] == recipe.blob:
                found = skills._dig(json.loads(b["body"]), recipe.items)
                if isinstance(found, list):
                    rows |= {json.dumps(x, sort_keys=True) for x in found if isinstance(x, dict)}
        return len(rows)
    for r in capture.get("responses", []):
        if r["method"] == recipe.method and urlsplit(r["url"]).path == path:
            found = skills._dig(json.loads(r["body"]), recipe.items)
            if isinstance(found, list):
                rows |= {json.dumps(x, sort_keys=True) for x in found if isinstance(x, dict)}
    return len(rows)


def _address_paths(item: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    """The dotted path of each text in one recorded item, with its text (objects only, a few levels deep)."""
    out: dict[str, str] = {}
    if depth > 3 or not isinstance(item, dict):
        return out
    for key, value in item.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, str) and len(value) <= 1000:
            out[path] = value
        elif isinstance(value, dict):
            out.update(_address_paths(value, path, depth + 1))
    return out


def find_apply(recipe_raw: Any, capture: dict) -> str | None:
    """Where, in one item of the recorded list, the role's own apply or employer address sits: the dotted path (not one
    the recipe already reads) whose text is an address `skills.employer_address` accepts (public https, off the
    board, not another board's) in at least `APPLY_SHARE` of at least two recorded items. None when no path does,
    and the board's roles then stay on the board. Found by code, never by a model; pure."""
    try:
        recipe = skills.parse_recipe(recipe_raw)
    except ValueError:
        return None
    if not isinstance(recipe, skills.RequestRecipe) or recipe.apply:
        return None
    path = urlsplit(_norm_url(recipe.url)).path
    items: list[dict] = []
    sources = ([(b["url"], b["body"]) for b in capture.get("embedded", []) if b.get("blob") == recipe.blob]
               if recipe.kind == "embedded" else [(r["url"], r["body"]) for r in capture.get("responses", [])
                                                  if r["method"] == recipe.method])
    for url, body in sources:
        if urlsplit(url).path != path:
            continue
        try:
            found = skills._dig(json.loads(body), recipe.items)
        except ValueError:
            continue
        items += [x for x in found if isinstance(x, dict)] if isinstance(found, list) else []
    if len(items) < 2:
        return None
    base, own = recipe.url_prefix or recipe.url, set(recipe.fields.values())
    counts: dict[str, int] = {}
    for item in items:
        for key, text in _address_paths(item).items():
            if key not in own and skills.employer_address(text, base):
                counts[key] = counts.get(key, 0) + 1
    ranked = sorted(k for k, n in counts.items() if n >= APPLY_SHARE * len(items))
    return ranked[0] if ranked else None


#: The share of a recording's items that must hold an employer's address at one path for the recipe to read it.
APPLY_SHARE = 0.6


def quality(postings: list[dict]) -> dict:
    """What a read's postings fill in: the share with a title, an address that is public, a posted date, and pay that is
    plausible when there is any."""
    n = len(postings) or 1
    paid = [p for p in postings if p.get("comp_min") or p.get("comp_max")]
    return {
        "rows": len(postings),
        "title": sum(1 for p in postings if p.get("title")) / n,
        "url": sum(1 for p in postings if p.get("url") and public_url(p["url"])) / n,
        "ext_id": sum(1 for p in postings if p.get("ext_id")) / n,
        "posted": sum(1 for p in postings if p.get("posted")) / n,
        "pay_plausible": (sum(1 for p in paid if PAY_RANGE[0] <= (p.get("comp_max") or p.get("comp_min") or 0)
                              <= PAY_RANGE[1]) / len(paid)) if paid else 1.0,
        "text": sum(1 for p in postings if p.get("text")) / n,
    }


def _tagged(capture: dict, method: str, path: str, tag: str) -> dict[search_fill.Place, str] | None:
    """The fields (`search_fill.leaves`) of the first recorded request to this endpoint that followed typing `tag`."""
    for r in capture.get("responses", []):
        if r.get("after") == tag and r["method"] == method and urlsplit(r["url"]).path == path:
            return search_fill.leaves(r["url"], r.get("post_data"))
    return None


def _last_tagged(capture: dict, method: str, path: str) -> dict[search_fill.Place, str] | None:
    """The fields of the request of the last typing before the recency filter's change that made one at this endpoint:
    the last place's, else the second title's, else the first's."""
    for tag in (*search_fill.PLACE_TAGS_LAST_FIRST, "second", "phrase"):
        if (got := _tagged(capture, method, path, tag)) is not None:
            return got
    return None


def place_ids(capture: dict, method: str, path: str, typed: dict | None) -> tuple[search_fill.Place, dict] | None:
    """For a board whose request carries the place as an id of its own (the typed place is nowhere in it): the field of
    the request at this endpoint that changed with the place and the board's value for each place picked from its own
    list of suggestions, found by code from the requests the picks made (`search_fill.place_values`). None when fewer
    than two places were picked, or no single field moved with them."""
    words = [w for w in [(typed or {}).get("place"), *((typed or {}).get("more_places") or [])] if w]
    requests: dict[str, dict[search_fill.Place, str]] = {}
    numbers: dict[search_fill.Place, bool] = {}
    for n, word in enumerate(words):
        sent = next((r for r in capture.get("responses", []) if r.get("after") == search_fill.place_tag(n)
                     and r["method"] == method and urlsplit(r["url"]).path == path), None)
        if sent is None:
            continue
        requests[word] = search_fill.leaves(sent["url"], sent.get("post_data"))
        for kind, name in requests[word]:
            if kind == "b" and search_fill.leaf_is_number(sent.get("post_data"), name):
                numbers[(kind, name)] = True
    return search_fill.place_values(requests, numbers)


def html_place_ids(pieces: list[dict], typed: dict | None) -> tuple[str, search_fill.Place, dict] | None:
    """For a board whose script calls answer with small pieces of HTML and carry the place as an id of its own: the
    endpoint (its path), the field of its address that changed with the place and the board's value for each place
    picked from its list of suggestions (`search_fill.place_values`, from the addresses of the pieces each pick made).
    None when no single field moved, or the addresses hold the places' own words."""
    words = [w for w in [(typed or {}).get("place"), *((typed or {}).get("more_places") or [])] if w]
    paths = dict.fromkeys(urlsplit(f["url"]).path for f in pieces)
    for path in paths:
        requests: dict[str, dict[search_fill.Place, str]] = {}
        for n, word in enumerate(words):
            piece = next((f for f in pieces if f.get("after") == search_fill.place_tag(n)
                          and urlsplit(f["url"]).path == path), None)
            if piece is not None:
                requests[word] = search_fill.leaves(piece["url"])
        if any(w.casefold() in v.casefold() for found in requests.values() for v in found.values() for w in words):
            continue
        if (got := search_fill.place_values(requests)) is not None:
            return path, got[0], got[1]
    return None


def _put_blank(recipe: dict, field: search_fill.Place, blank: str) -> dict:
    """The recipe with `blank` where the request field is: the address's query entry, a `params` entry, or the body's
    value at that dotted path."""
    kind, name = field
    if kind == "q":
        url = str(recipe.get("url") or "")
        parts = urlsplit(url)
        entries, in_address = [], False
        for entry in parts.query.split("&") if parts.query else []:
            key, mark, _ = entry.partition("=")
            if mark and unquote_plus(key) == name:
                entries.append(f"{key}={blank}")
                in_address = True
            else:
                entries.append(entry)
        if in_address:
            return {**recipe, "url": parts._replace(query="&".join(entries)).geturl().replace(
                blank.replace("{", "%7B").replace("}", "%7D"), blank)}
        return {**recipe, "params": {**(recipe.get("params") or {}), name: blank}}
    body = json.loads(json.dumps(recipe.get("body"))) if isinstance(recipe.get("body"), dict) else None
    if body is None or not name:
        return recipe
    node: Any = body
    steps = name.split(".")
    for step in steps[:-1]:
        node = node[int(step)] if isinstance(node, list) and step.isdigit() and int(step) < len(node) else (
            node.get(step) if isinstance(node, dict) else None)
        if node is None:
            return recipe
    last = steps[-1]
    if isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = blank
    elif isinstance(node, dict):
        node[last] = blank
    else:
        return recipe
    return {**recipe, "body": body}


def with_place_map(recipe_raw: Any, capture: dict, typed: dict | None) -> Any:
    """The recipe with `{place}` where the board's request carries the place, and its `place_map`, made by code from the
    recording (never a model's), for a board whose request has no typed place in it but an id the board's own list of
    suggestions gave for each place picked (`place_ids`). A board whose request holds the typed place itself needs no
    map,
    and a recipe made for it is returned without one. Anything but a recipe is returned as it is."""
    if not isinstance(recipe_raw, dict):
        return recipe_raw
    out = {k: v for k, v in recipe_raw.items() if k != "place_map"}
    if not typed or not typed.get("place"):
        return out
    method, path = str(out.get("method") or "GET").upper(), urlsplit(str(out.get("url") or "")).path
    if search_fill.blank_places(_tagged(capture, method, path, "phrase"), _tagged(capture, method, path, "second"),
                                _tagged(capture, method, path, "place"), typed)["place"]:
        return out  # the board's request holds the place's own words
    found = place_ids(capture, method, path, typed)
    if found is None:
        return out
    field, mapping = found
    return {**_put_blank(out, field, "{place}"), "place_map": mapping}


def blank_problems(recipe: skills.RequestRecipe, capture: dict, typed: dict | None) -> list[str]:
    """What is wrong with the way a recipe uses the typed words: a blank with no word typed for it, a blank where the
    recorded searches did not differ, and a typed word still whole in the recipe. Empty when the recipe is clean (a
    recipe with no blanks and none of the words in it). `{since}` is held to the recording's change of the recency
    filter in the same way, and a recording that found the filter's field in the request the recipe is made from asks
    the recipe to use `{since}` there."""
    problems: list[str] = []
    blanks = recipe.blanks()
    for name in sorted(blanks):
        if name == "since":
            if search_fill.since_spec(typed) is None:
                problems.append("{since} is used, but the recording found no recency filter: leave it out")
        elif not (typed or {}).get("phrase" if name == "phrase" else "place"):
            problems.append(f"{{{name}}} is used, but nothing was typed for it in the recording: leave it out")
    if typed and (blanks or typed.get("since")):
        path = urlsplit(recipe.url).path
        where = search_fill.blank_places(
            _tagged(capture, recipe.method, path, "phrase"), _tagged(capture, recipe.method, path, "second"),
            _tagged(capture, recipe.method, path, "place"), typed,
            last=_last_tagged(capture, recipe.method, path), with_since=_tagged(capture, recipe.method, path, "since"))
        if not where["place"] and (ids := place_ids(capture, recipe.method, path, typed)) is not None:
            where["place"] = {ids[0]}  # the board's own id for the place, from its own list of suggestions
        held = search_fill.blank_fields(recipe.url, recipe.params, recipe.body)
        shown = {name: ", ".join(sorted(f"{kind}:{field or '(whole body)'}" for kind, field in found))
                 for name, found in where.items()}
        if "since" in blanks and search_fill.since_spec(typed) is not None:
            if not where.get("since"):
                problems.append("the recording shows no field of the request that moved with the recency filter: "
                                "{since} cannot be used for this site")
            elif stray := held["since"] - where["since"]:
                problems.append(f"{{since}} is in {', '.join(sorted(f'{k}:{f}' for k, f in stray))}, but the recency "
                                f"filter moved {shown['since']}: put it there")
        elif "since" not in blanks and where.get("since"):
            problems.append(f"the recording changed the site's recency filter, and the request it searches with holds "
                            f"it in {shown['since']}: write {{since}} there, never the recorded value")
        for name in sorted(blanks & {"phrase", "place"}):
            if not typed.get("phrase" if name == "phrase" else "place"):
                continue
            if not where[name]:
                problems.append(f"the recording shows no field of the request that held the typed {name}: "
                                f"{{{name}}} cannot be used for this site")
            elif stray := held[name] - where[name]:
                shown = ", ".join(sorted(f"{kind}:{field or '(whole body)'}" for kind, field in stray))
                right = ", ".join(sorted(f"{kind}:{field or '(whole body)'}" for kind, field in where[name]))
                problems.append(f"{{{name}}} is in {shown}, but the recorded searches differ in {right}: put it there")
    for word in search_fill.literals_left(recipe.url, recipe.params, recipe.body, typed):
        problems.append(f"the word typed in the recording ({word!r}) is still in the request: use the blank "
                        "({phrase} or {place}) where it goes, never the word")
    return problems


def typed_search(typed: dict | None, blanks: set[str]) -> search_fill.Search:
    """The search a recipe's blanks are filled with to replay the recording: the typed title, and the typed place when
    the recipe has a `{place}`. With a `{since}` the replay is of the request the recency filter's change made, which
    followed the last title typed and holds the filter's recorded value exactly."""
    phrase, place = (typed or {}).get("phrase"), (typed or {}).get("place")
    since = ((typed or {}).get("since") or {}).get("after") if "since" in blanks else None
    if since:
        phrase = (typed or {}).get("second") or phrase
        place = ((typed or {}).get("more_places") or [place])[-1]  # the form held the last place picked
    return search_fill.Search(tuple([phrase] if phrase else []), tuple([place] if place and "place" in blanks else []),
                              since_literal=since)


def with_since_spec(recipe_raw: Any, capture: dict, typed: dict | None) -> Any:
    """The recipe with its `since` entry made by code from what the recording learned of the recency filter (its format
    and choices, never a model's), when the recipe writes `{since}`; without it, none. Whether the filter's field is a
    JSON number in the request the page made is read from the capture. Anything but a recipe is returned as it is."""
    if not isinstance(recipe_raw, dict):
        return recipe_raw
    out = {k: v for k, v in recipe_raw.items() if k != "since"}
    spec = search_fill.since_spec(typed)
    holds = skills.placeholders_of([recipe_raw.get("url"), recipe_raw.get("params"), recipe_raw.get("body")])
    if spec is None or "since" not in holds:
        return out
    path = urlsplit(str(recipe_raw.get("url") or "")).path
    where = search_fill.blank_fields(str(recipe_raw.get("url") or ""), recipe_raw.get("params"), recipe_raw.get("body"))
    sent = next((r for r in capture.get("responses", []) if r.get("after") == "since"
                 and urlsplit(r["url"]).path == path), None)
    number = bool(sent and spec["format"] in ("seconds", "days") and any(
        kind == "b" and search_fill.leaf_is_number(sent.get("post_data"), field) for kind, field in where["since"]))
    return {**out, "since": {**spec, **({"number": True} if number else {})}}


async def verify_offline(recipe_raw: Any, capture: dict, typed: dict | None = None) -> Verdict:
    """Run a recipe against the capture and say whether it reproduced the recorded list. Never raises. `typed` is what
    the recording typed (`search_fill.clean_typed`); by default the capture's own. A recipe with search blanks is
    also held to `blank_problems` and replayed exactly: filled with the typed words, its first request must be the
    one the page made."""
    typed = typed if typed is not None else capture.get("typed")
    if isinstance(recipe_raw, dict) and search_fill.since_spec(typed) is None and "since" in skills.placeholders_of(
            [recipe_raw.get("url"), recipe_raw.get("params"), recipe_raw.get("body")]):
        return Verdict(False, ["{since} is used, but the recording found no recency filter: leave it out"])
    try:
        recipe = skills.parse_recipe(with_since_spec(with_place_map(recipe_raw, capture, typed), capture, typed))
    except ValueError as exc:
        return Verdict(False, [f"the recipe is not valid: {exc}"])
    if not isinstance(recipe, skills.RequestRecipe):
        return Verdict(False, ["a recipe written from a recording is a request or an embedded recipe"])
    problems: list[str] = blank_problems(recipe, capture, typed)
    blanks = recipe.blanks()
    if recipe.detail is not None:
        pattern = _detail_pattern(recipe.detail.url)
        if not any(pattern.fullmatch(_norm_url(r["url"])) for r in capture.get("responses", [])):
            problems.append("the detail request was never recorded: leave detail out, or use an address the page "
                            "called")
    if problems and blanks:  # a blank that is wrong is not replayed: it would only say so again
        return Verdict(False, problems, 0, 0)
    recorded = _recorded_rows(recipe, capture)
    fetch = ReplayFetcher(capture, first_exact=bool(blanks))
    try:
        listing = await skills.run_recipe(cast(Fetcher, fetch), recipe, search=typed_search(typed, blanks))
    except (skills.SkillError, ValueError) as exc:
        return Verdict(False, [*problems, f"replaying it failed: {exc}"], 0, recorded)
    if blanks and fetch.hit is not None:  # the rows of the one search it reproduced, not of every search recorded
        found = skills._dig(json.loads(fetch.hit["body"]), recipe.items)
        recorded = len({json.dumps(x, sort_keys=True) for x in found if isinstance(x, dict)}) if isinstance(
            found, list) else 0
    stats = quality(listing.postings)
    if recorded == 0:
        problems.append("the recording holds no rows at that endpoint and list path")
    if not listing.postings:
        problems.append("the recipe read no postings from the recording")
    elif recorded and len(listing.postings) < FILL_MIN * recorded:
        problems.append(f"it reproduced {len(listing.postings)} of the {recorded} recorded rows")
    for name in ("title", "url", "ext_id"):
        if listing.postings and stats[name] < FILL_MIN:
            problems.append(f"{name} is filled in for {stats[name]:.0%} of the postings; it must be {FILL_MIN:.0%}")
    if stats["pay_plausible"] < FILL_MIN:
        problems.append("the pay it reads is not plausible as a yearly figure")
    if "posted" in recipe.fields and listing.postings and stats["posted"] < 0.5:
        problems.append("the posted dates do not parse for half of the postings: add a date transform")
    if listing.postings:  # the roles the page showed are the roles the recipe reads (PRD 37.17)
        problems += same_roles.problems(listing.postings, capture.get("cards"))
    return Verdict(not problems, problems, len(listing.postings), recorded, stats)


async def verify_posting_offline(recipe_raw: Any, capture: dict, sample_url: str) -> Verdict:
    """Run a host's posting recipe against a recording of one posting's page (`sample_url`) and say whether it gave that
    posting's whole text: the recipe covers the sample's host, its pattern names the posting's id in the sample's
    address, its detail request is one the page made (with that id in it), and replaying it reads a whole text. Never
    raises."""
    try:
        recipe = skills.parse_recipe(recipe_raw)
    except ValueError as exc:
        return Verdict(False, [f"the recipe is not valid: {exc}"])
    if not isinstance(recipe, skills.PostingRecipe):
        return Verdict(False, ["a recipe written from a posting's page is a posting recipe (kind: posting)"])
    host = urlsplit(sample_url).hostname or ""
    if not skills.host_covers(recipe.host, host):
        return Verdict(False, [f"host {recipe.host} does not cover the recorded page's host {host}"])
    match = skills.compile_pattern(recipe.url_pattern, "url_pattern").fullmatch(sample_url.split("#", 1)[0])
    if match is None or not match.group("ext_id"):
        return Verdict(False, [f"url_pattern does not match the recorded page's address {sample_url} whole, with the "
                               "posting's id in (?P<ext_id>...)"])
    pattern = _detail_pattern(recipe.detail.url)
    if not any(pattern.fullmatch(_norm_url(r["url"])) for r in capture.get("responses", [])):
        return Verdict(False, ["the detail request was never recorded: use an address the page called, with the "
                               "posting's id where {ext_id} is"])
    replay = ReplayFetcher(capture)
    try:
        got = await skills.read_posting(cast(Fetcher, replay), recipe, sample_url)
    except (skills.SkillError, ValueError) as exc:
        return Verdict(False, [f"replaying it failed: {exc}"])
    if replay.unrecorded:
        return Verdict(False, ["the detail request for the recorded posting was not one the page made: use the "
                               "address the page called, with the posting's id where {ext_id} is"])
    text = (got.text if got is not None else None) or ""
    if not text:
        return Verdict(False, ["the detail request gave no description: check the path of the description"])
    if (why := whole.why_not(text)) is not None:
        return Verdict(False, [f"the text it read is not a whole posting ({why})"])
    return Verdict(True, [], 1, 1, {"text": len(text)})


# --- health -----------------------------------------------------------------------------------------------------------

BLOCKED_STATUSES = (401, 403, 429)


def blocked(exc: BaseException) -> bool:
    """A refusal or a block: a stop that cools the site, never a reason to rewrite the recipe. A run the refusal
    table ended (`sites/refusals.py`: rests in a row, a sign-in page, a bot check) is marked `refused`, whatever its
    status."""
    from app.jobsearch.fetch.polite import Refused

    return (isinstance(exc, Refused) or getattr(exc, "refused", False) is True
            or getattr(exc, "status", None) in BLOCKED_STATUSES)


def judge_night(history: list[dict], rows: int, stats: dict | None, error: str | None,
                said_empty: bool = False) -> str | None:
    """Why a night's read looks broken, or None. `history` is the earlier nights' entries ({rows, ...}, oldest first).
    Zero rows is a failure, never "nothing new"; a fall far under the recent median is one too. Except a read the site
    answered by saying there is nothing (`said_empty`: the refusal table found its empty-results words, or a proper
    empty list for a search, `sites/refusals.py`): that is a good night however many it read before, never a bad one."""
    if error:
        return error
    if said_empty and rows == 0:
        return None
    if rows == 0:
        return "it read no postings"
    recent = [h["rows"] for h in history[-HISTORY:] if h.get("rows")]
    if len(recent) >= MEDIAN_NEEDS and rows < DROP_BELOW * statistics.median(recent):
        return f"it read {rows} postings against a recent median of {int(statistics.median(recent))}"
    if stats:
        for name in ("title", "url", "ext_id"):
            if stats.get(name, 1.0) < FILL_MIN:
                return f"{name} is filled in for {stats[name]:.0%} of the postings"
    return None
