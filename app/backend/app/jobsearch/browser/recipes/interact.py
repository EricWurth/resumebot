"""Working a listing page the way a visitor would, while a recording is made (`recipe_record.capture_page`).

A site that renders its first results on the server and calls its search API only when a visitor interacts shows a
passive load nothing. So the recording's tab, in the app's own Chrome, does a handful of things a visitor does on a
public page, once each and in this order, and then stops: run the site's own job search (its search box, first left as
it is, an empty query; only when that brings no results does one keyword from the owner's own target roles go in, given
by the caller, never a word in code), go to the next page of results, apply one filter, and open one job.

Bounds. At most `ACTIONS` actions and `INTERACT_SECONDS` seconds in all, `ACTION_GAP` seconds between two actions
(about a request a second), and a step that is not there is skipped. Each step first *finds* its element in the page
(a small script that clicks nothing and marks the element), and the code vets what it found before anything is
clicked: a link goes to a public address on the page's own host, robots.txt allows it, and it is not a sign-in, a
registration, an account or an application. Nothing is typed but that one keyword, in the search box; or, for the first
recording of a site the owner added (`phrases`, `place`), the owner's first title, then the second one when there is
one, then the first place, each in a text box of the site's own job-search form (the box vetted as any element is,
never on a page that asks for a password, no click but the one that submits the form; the one more click the owner
allowed, 2026-11-17: when the site shows its own list of suggestions under a place box, the entry that is exactly the
typed place, or starts with it, is picked, and no other; no match, no pick), each followed by a look at what the
page asked for (`Outcome.after`), so that code can tell which field of the page's own request held the typed word
(`sites/search_fill.py`). Then, once, the site's own recency filter (a list, a
date or a number of days or seconds in its job-search form, whose label says it is about when a role was posted) is
changed to a value other than the one it had, and looked at the same way, so that code can tell which field of the
request moved with it (`{since}`); never a field that is not part of the search form, never one of a sign-in, an
application or a post. No form is
submitted but the site's search box, none that asks for a password, and no cookie or consent control, newsletter or
alert is touched, except one: a cookie banner is declined, once, before the steps (`decline_consent`; the owner,
2026-11-04: deny the tracking cookies and move on), by clicking only its control that refuses non-essential cookies
("Reject all", "Decline", "Necessary only"), never one that accepts, and never counted among the actions. A banner with
no such control is left, and the steps go on under it. After each action the
traffic is looked at: a refusal (401, 403 or 429 on a page, 429 on any request to the host) is a stop for the whole
recording.

What the page's data requests were answered is read by the caller from the same Network events as before; what this
module adds is each step's name and what the page's HTML held afterwards (`Snapshot`), for the fallback that reads
data embedded in the page. Nothing here is a model's: the steps are code, and the scripts below read the page, never
run what the page says."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.jobsearch import consent
from app.jobsearch.browser import code as bc
from app.jobsearch.fetch.polite import public_url
from app.sites import registry, search_fill

#: The most actions, the most seconds, the pause between two actions, and how long the page is given to answer one.
ACTIONS = 4
INTERACT_SECONDS = 90.0
ACTION_GAP = 1.0
ACTION_SETTLE = 4.0
#: A page that builds its results in the browser is given this long (looked at once a second) to show the element a step
#: looks for; the looking is the browser's own page, never a request to the site.
FIND_WAIT = 10.0
#: The most of a page's HTML kept for reading embedded data.
HTML_MAX = 8_000_000
STEPS = ("search", "next", "filter", "open")
MARK = "data-rb-pick"
BLOCKED_STATUSES = (401, 403, 429)
#: What a link or control may never be: an account, a sign-in, an application, a consent or a subscription.
NEVER = (r"sign[\s-]*in|log[\s-]*in|login|sign[\s-]*up|register|account|password|apply|application|cookie|consent|"
         r"privacy|newsletter|subscribe|alert|terms|remember|captcha")

_PRELUDE = f"""
const NEVER = new RegExp({json.dumps(NEVER)}, 'i');
const seen = (el) => {{ const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
  if (s.visibility === 'hidden' || s.display === 'none' || el.closest('[hidden], [aria-hidden=true]')) return false;
  return (r.width > 0 && r.height > 0) || (el.innerText || '').trim().length > 0; }};
const said = (el) => [el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('name'),
  el.getAttribute('placeholder'), el.id, typeof el.className === 'string' ? el.className : '', el.getAttribute('href')]
  .filter(Boolean).join(' ').slice(0, 300);
const unsafeForm = (el) => {{ const f = el.closest('form'); if (!f) return false;
  return !!f.querySelector('input[type=password]') ||
    NEVER.test((f.getAttribute('action') || '') + ' ' + (f.id || '')); }};
const sameHost = (href) => {{ try {{ const u = new URL(href, location.href); return u.hostname === location.hostname; }}
  catch (e) {{ return false; }} }};
document.querySelectorAll('[{MARK}]').forEach((e) => e.removeAttribute('{MARK}'));
const pick = (el, what) => {{ el.setAttribute('{MARK}', '1');
  const href = el.tagName === 'A' ? new URL(el.getAttribute('href'), location.href).href : null;
  return JSON.stringify({{found: true, what: what, href: href}}); }};
const none = JSON.stringify({{found: false}});
"""

_FIND = {
    "search": """
const KEYWORDY = /keyword|search|job|title|role|position|what|query|find/i;
const PLACE = /location|city|zip|postal|where|distance|radius|email|phone/i;
const labelOf = (el) => ((el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' +
  (el.getAttribute('aria-labelledby') || '').split(' ').map((i) => (document.getElementById(i) || {}).innerText || '')
  .join(' '));
const inRegion = (el) => !!el.closest('[role=search], form[action*=search i], [class*=search i], [id*=search i]');
const boxes = [...document.querySelectorAll('input')].filter((el) => {
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  if (!['search', 'text'].includes(type) || !seen(el) || el.disabled || el.readOnly || unsafeForm(el)) return false;
  const said_ = said(el) + ' ' + labelOf(el);
  return !PLACE.test(said_) && (KEYWORDY.test(said_) || inRegion(el));
});
const el = boxes.find((b) => KEYWORDY.test(said(b) + ' ' + labelOf(b))) || boxes[0];
if (el) return pick(el, 'search box');
const WORDS = new RegExp('^(search( (all )?(jobs?|openings|roles|positions))?|find (jobs?|roles?|openings)|' +
  '(view|see|browse) (all )?(jobs?|openings|roles|positions))$', 'i');
const btn = [...document.querySelectorAll('a[href], button, [role=button], input[type=submit]')].filter((b) => {
  if (!seen(b) || b.disabled || NEVER.test(said(b)) || unsafeForm(b)) return false;
  if (b.tagName === 'A' && !sameHost(b.getAttribute('href'))) return false;
  const text = ((b.innerText || b.value || '') + '').trim();
  return WORDS.test(text) || WORDS.test((b.getAttribute('aria-label') || '').trim());
})[0];
if (!btn) return none;
return pick(btn, 'search box');
""",
    "place": """
const PLACE = /location|city|where/i;
const labelOf = (el) => ((el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' +
  (el.getAttribute('aria-labelledby') || '').split(' ').map((i) => (document.getElementById(i) || {}).innerText || '')
  .join(' '));
const el = [...document.querySelectorAll('input')].find((el) => {
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  if (!['search', 'text'].includes(type) || !seen(el) || el.disabled || el.readOnly || unsafeForm(el)) return false;
  const said_ = said(el) + ' ' + labelOf(el);
  return PLACE.test(said_) && !NEVER.test(said_);
});
if (!el) return none;
const plain = !el.getAttribute('list') && !el.getAttribute('aria-autocomplete') &&
  !['combobox', 'searchbox'].includes((el.getAttribute('role') || '').toLowerCase()) &&
  !el.getAttribute('aria-haspopup') && !el.getAttribute('aria-controls') && !el.closest('[role=combobox]');
return JSON.stringify({...JSON.parse(pick(el, 'place box')), plain: plain});
""",
    "next": """
const cands = [...document.querySelectorAll('a[href], button, [role=button], [role=link]')].filter((el) => {
  if (!seen(el) || el.disabled || el.getAttribute('aria-disabled') === 'true' || NEVER.test(said(el))) return false;
  if (el.tagName === 'A' && !sameHost(el.getAttribute('href'))) return false;
  const text = (el.innerText || '').trim();
  const label = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
  return el.matches('a[rel~=next]') || /^(next|next page|[\\u203a\\u00bb>\\u2192])$/i.test(text) ||
    /^(view )?next( page)?$/i.test(label);
});
const el = cands[0];
if (!el) return none;
return pick(el, 'next page');
""",
    "filter": """
const boxes = [...document.querySelectorAll('input[type=checkbox]')].filter((el) => {
  if (!seen(el) || el.disabled || el.checked || unsafeForm(el)) return false;
  if (el.closest('[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i], [id*=onetrust i], ' +
    '[id*=didomi i], [id*=usercentrics i]')) return false;  // a banner's switches are cookies, never a filter
  const label = (el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' + said(el);
  return !NEVER.test(label);
});
const inFacet = boxes.filter((el) => !!el.closest(
  '[class*=filter i], [class*=facet i], [class*=refine i], [id*=filter i], [id*=facet i], aside'));
const el = (inFacet.length ? inFacet : boxes)[0];
if (!el) return none;
return pick(el, 'filter');
""",
    "recency": """
const RECENT = /posted|post date|date posted|recen|within|last|since|fresh|how old|newest|published|time ?frame|date/i;
const OTHER = new RegExp('location|city|zip|postal|distance|radius|mile|country|state|salary|pay|sort|order|type|' +
  'level|department|categor|company|employer|remote|keyword|language|birth|start|end', 'i');
const labelOf = (el) => ((el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' +
  (el.getAttribute('aria-labelledby') || '').split(' ').map((i) => (document.getElementById(i) || {}).innerText || '')
  .join(' '));
const named = (el) => [el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('name'),
  el.getAttribute('placeholder'), el.id, labelOf(el)].filter(Boolean).join(' ').slice(0, 300);
const inSearch = (el) => !!el.closest('form, [role=search], [class*=search i], [class*=filter i], [class*=facet i], ' +
  '[id*=search i], [id*=filter i], aside');
const el = [...document.querySelectorAll('select, input[type=date], input[type=number]')].find((el) => {
  if (!seen(el) || el.disabled || el.readOnly || unsafeForm(el) || !inSearch(el)) return false;
  const t = named(el);
  if (OTHER.test(t) || NEVER.test(t)) return false;
  if (el.tagName !== 'SELECT') return RECENT.test(t);
  const spans = [...el.options].filter((o) => /(\\d+|an?|one)?\\s*(hour|day|week|month)/i.test(o.text)).length;
  return RECENT.test(t) || spans >= 2;
});
if (!el) return none;
const kind = el.tagName === 'SELECT' ? 'select' : el.type;
const options = kind === 'select' ? [...el.options].slice(0, 40).map((o) => ({value: o.value, label: o.text})) : [];
return JSON.stringify({...JSON.parse(pick(el, 'recency filter')), kind: kind, value: el.value || '',
  hint: named(el), options: options});
""",
    "open": """
const jobs = new RegExp('/(jobs?|job-?details?|positions?|openings?|requisitions?|vacanc[a-z]*|careers?|' +
  'postings?)/[^/?#]{3,}', 'i');
const cands = [...document.querySelectorAll('a[href]')].filter((el) => {
  const href = el.getAttribute('href') || '';
  const text = (el.innerText || '').trim();
  return seen(el) && sameHost(href) && jobs.test(href) && !NEVER.test(href) && !NEVER.test(text) &&
    text.length >= 6 && text.length <= 160;
});
const el = cands[0];
if (!el) return none;
return pick(el, 'a job');
""",
}

# A cookie banner's controls, found by what the banner calls itself (its id, class or label) or, for a dialog, by what
# it says; nothing is clicked here (`consent_find.js`). Each control that is in a banner is marked with its number and
# answered with its label, so the code (`consent.is_decline`) can tell which one refuses cookies. A banner is a small
# element that talks about cookies or tracking: the page's own wrapper (a class like "has-cookie-banner" on the whole
# app) is too big to be one. `consent_click.js` clicks the control marked with `%NUMBER%` and takes every mark off.
# Both are files beside this module, read once, so the scout's node tests (`app/scout/consent-find.test.mjs`) run the
# very text the page gets.
_SCRIPTS = Path(__file__).parent
_CONSENT_FIND = (_SCRIPTS / "consent_find.js").read_text(encoding="utf-8")
_CONSENT_CLICK = (_SCRIPTS / "consent_click.js").read_text(encoding="utf-8")


_CLICK = f"""
(() => {{ const el = document.querySelector('[{MARK}]'); if (!el) return JSON.stringify({{clicked: false}});
  el.removeAttribute('{MARK}');
  if (el.tagName === 'A') el.target = '_self';
  if (el.tagName === 'INPUT' && el.type !== 'checkbox') {{
    el.focus();
    const form = el.form;
    const near = (form || el.closest('div, section, header')) &&
      [...(form || el.closest('div, section, header')).querySelectorAll('button, input[type=submit], [role=button]')]
      .find((b) => /search|find|go|submit/i.test(
        (b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' + (b.value || '')));
    if (near) near.click();
    else if (form && form.requestSubmit) form.requestSubmit();
    else {{ for (const t of ['keydown', 'keypress', 'keyup'])
      el.dispatchEvent(new KeyboardEvent(t, {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}})); }}
  }} else el.click();
  return JSON.stringify({{clicked: true}}); }})()
"""

def _type_script(word: str) -> str:
    """Types one word in the marked search box, the way a visitor does (the page hears an input and a change)."""
    return (f"(() => {{ const el = document.querySelector('[{MARK}]'); "
            "if (!el || el.tagName !== 'INPUT') return JSON.stringify({typed: false}); el.focus(); "
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, "
            f"{json.dumps(word)}); "
            "for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true})); "
            "return JSON.stringify({typed: true}); })()")


#: The attribute that marks one entry of a site's own list of suggestions while its text is read.
OPT_MARK = "data-rb-opt"
#: How many times the page is looked at (a second apart) for the list of suggestions to show after a place is typed.
SUGGEST_POLLS = 4
#: The most entries of a list that are read.
SUGGEST_MAX = 30

#: Reads the entries of the site's own list of suggestions for the marked box (the list its `aria-controls` or
#: `aria-owns` names, else a visible listbox), marks each with its number and answers their texts. Nothing is clicked.
_SUGGEST_LIST = ("(() => { const NEVER = new RegExp(" + json.dumps(NEVER) + ", 'i'); "
                 "const seen = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); "
                 "return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0; }; "
                 "document.querySelectorAll('[" + OPT_MARK + "]')"
                 ".forEach((e) => e.removeAttribute('" + OPT_MARK + "')); "
                 "const box = document.querySelector('[" + MARK + "]'); "
                 "if (!box) return JSON.stringify({options: []}); "
                 "let lists = [box.getAttribute('aria-controls'), box.getAttribute('aria-owns')].filter(Boolean)"
                 ".flatMap((i) => i.split(' ')).map((i) => document.getElementById(i)).filter(Boolean); "
                 "if (!lists.length) lists = [...document.querySelectorAll('[role=listbox]')]; "
                 "const out = []; "
                 "for (const list of lists.filter(seen)) { "
                 "let items = [...list.querySelectorAll('[role=option]')]; "
                 "if (!items.length) items = [...list.querySelectorAll('li')]; "
                 "for (const el of items) { if (out.length >= " + str(SUGGEST_MAX) + " || !seen(el)) continue; "
                 "const text = (el.innerText || '').trim().replace(/\\s+/g, ' '); "
                 "if (!text || text.length > 120 || NEVER.test(text)) continue; "
                 "const a = el.matches('a[href]') ? el : el.querySelector('a[href]'); "
                 "if (a) { try { if (new URL(a.getAttribute('href'), location.href).hostname !== location.hostname) "
                 "continue; } catch (e) { continue; } } "
                 "el.setAttribute('" + OPT_MARK + "', String(out.length)); out.push(text); } } "
                 "return JSON.stringify({options: out}); })()")


def _pick_script(index: int) -> str:
    """Clicks the one entry of a suggestion list that `_SUGGEST_LIST` marked with this number, and nothing else: an
    element that is not an option of a list is never clicked."""
    return ("(() => { const el = document.querySelector('[" + OPT_MARK + "=\"" + str(int(index)) + "\"]'); "
            "if (!el || !(el.matches('[role=option]') || el.closest('[role=listbox], ul, ol'))) "
            "return JSON.stringify({picked: false}); el.removeAttribute('" + OPT_MARK + "'); "
            "for (const t of ['mousedown', 'mouseup', 'click']) "
            "el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true})); "
            "return JSON.stringify({picked: true}); })()")


def _choose_script(value: str) -> str:
    """Chooses the option with this value in the marked list, the way a visitor does (the page hears an input and a
    change)."""
    return ("(() => { const el = document.querySelector('[" + MARK + "]'); "
            "if (!el || el.tagName !== 'SELECT') return JSON.stringify({set: false}); "
            f"const want = {json.dumps(value)}; if (![...el.options].some((o) => o.value === want)) "
            "return JSON.stringify({set: false}); el.focus(); el.value = want; "
            "for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true})); "
            "return JSON.stringify({set: true}); })()")


#: Submits the form of the marked filter when changing it asked the site for nothing: the form's own search button
#: (never one that says sign in, apply or the like), else the form itself.
_SUBMIT = ("(() => { const NEVER = new RegExp(" + json.dumps(NEVER) + ", 'i'); "
           "const el = document.querySelector('[" + MARK + "]'); if (!el) return JSON.stringify({clicked: false}); "
           "const form = el.form; const scope = form || el.closest('div, section, aside, header'); "
           "const said = (b) => ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' + "
           "(b.value || '')); "
           "const btn = scope && [...scope.querySelectorAll('button, input[type=submit], [role=button]')]"
           ".find((b) => /search|find|update|filter|show|go|submit/i.test(said(b)) && !NEVER.test(said(b))); "
           "if (btn) btn.click(); else if (form && form.requestSubmit) form.requestSubmit(); "
           "else return JSON.stringify({clicked: false}); return JSON.stringify({clicked: true}); })()")

#: How far back a recording sets a recency filter that takes a number or a date (a week).
RECENCY_DAYS = 7

#: Whether the page shows a field for a password: nothing is typed on such a page.
_PASSWORD = ("(() => { const shown = [...document.querySelectorAll('input[type=password]')].some((el) => { "
             "const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }); "
             "return JSON.stringify({password: shown}); })()")

_HERE = "JSON.stringify({url: location.href})"

_HTML = ("JSON.stringify({url: location.href, "
         f"html: document.documentElement.outerHTML.slice(0, {HTML_MAX})}})")


@dataclass
class Snapshot:
    """The page as one step left it: its address and its HTML."""

    url: str
    html: str


@dataclass
class Outcome:
    """What the interaction did: the steps taken (`done`), the steps skipped with why (`skipped`), the page as it stood
    after the load and after each step (`snapshots`), the status of a refusal that stopped it (`stop`), and whether a
    cookie banner was declined first (`declined`, no step of its own)."""

    done: list[str]
    skipped: dict[str, str]
    snapshots: list[Snapshot]
    stop: int | None = None
    declined: bool = False
    #: What the visitor typed (`search_fill.clean_typed`'s shape: phrase, second, place), None when nothing was, and
    #: which typing each data request followed (request id -> one of `search_fill.AFTER`).
    typed: dict | None = None
    after: dict[str, str] = field(default_factory=dict)
    #: The page's address just before the recency filter was changed (None when it was not): with the address after it,
    #: the entry of the address that moved with the filter.
    before_url: str | None = None
    #: The page's address after each place was picked from the site's own list (the place typed -> the address), for the
    #: query entry that holds the board's own value for the place (`search_fill.place_values`).
    place_urls: dict[str, str] = field(default_factory=dict)


#: Hosts that are no job site at all: where a page links to its social profiles, its maps and its stores. The job
#: boards are the registry's (`not_boards`).
NOT_A_JOB_SITE = ("facebook.", "twitter.", "x.com", "youtube.", "instagram.", "apple.com", "microsoft.com", "w3.org",
                  "schema.org", "github.", "tiktok.", "pinterest.")


def not_boards() -> tuple[str, ...]:
    """What a host is never taken for a job board's page by: the sites that are none, and the job boards (a board
    named by its first label and a dot: `example.` for `example.test`), as the registry lists them."""
    return (*NOT_A_JOB_SITE, *(h.removeprefix("www.").split(".")[0] + "." for h in registry.board_hosts()))


_URL = re.compile(r"https://[A-Za-z0-9.-]+(?:/[^\s\"'<>\\)]*)?")
_BOARD_PATH = re.compile(r"/(job-?boards?|jobs?|openings?|positions?)(/[^/]+)?/?", re.I)
_CAREER_PATH = re.compile(r"/careers?(/[^/]+)?/?", re.I)


def board_link(html: str, page_url: str) -> str | None:
    """Where the jobs are, when the employer's own page only points to them: the first public address on another host
    that the page itself names (a link, a button's target, a frame) and whose path is a job board's (`/job-board/x`,
    `/jobs`, `/openings`; a `/careers` path only when there is no better). One hop, never a social site or a store.
    The caller still asks robots.txt before it goes there."""
    here = urlsplit(page_url).hostname or ""
    text = html.replace("\\/", "/").replace("&amp;", "&")
    never = not_boards()
    best: tuple[int, str] | None = None
    for m in _URL.finditer(text):
        link = m.group(0).rstrip(".,;")
        host = urlsplit(link).hostname or ""
        if not host or host == here or any(n in host for n in never) or not public_url(link):
            continue
        path = urlsplit(link).path
        rank = 0 if _BOARD_PATH.fullmatch(path) else 1 if _CAREER_PATH.fullmatch(path) else None
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, link)
    return best[1] if best else None


def refused(requests: list[dict], host: str, statuses: tuple[int, ...] = BLOCKED_STATUSES) -> int | None:
    """The status of a refusal in what the page asked for: a page answered 401, 403 or 429 (or one of `statuses`, the
    refusal table's: `sites/refusals.py`), or any request to the page's own host answered 429."""
    for r in requests:
        status = r.get("status")
        if r.get("kind") == "Document" and status in statuses:
            return status
        if status == 429 and (urlsplit(r.get("url") or "").hostname or "") == host:
            return 429
    return None


async def _value(tools: Any, sid: str, expression: str) -> Any:
    got = await tools.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, sid)
    value = (got.get("result") or {}).get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def snapshot(tools: Any, sid: str) -> Snapshot | None:
    """The page's address and HTML now, or None when it cannot be read."""
    got = await _value(tools, sid, _HTML)
    if isinstance(got, dict) and isinstance(got.get("html"), str):
        return Snapshot(str(got.get("url") or ""), got["html"])
    return None


async def find(tools: Any, sid: str, step: str) -> dict | None:
    """The element a step would use, marked in the page (nothing clicked): {found, what, href}, or None."""
    got = await _value(tools, sid, f"(() => {{ {_PRELUDE} {_FIND[step]} }})()")
    return got if isinstance(got, dict) and got.get("found") else None


async def click(tools: Any, sid: str) -> bool:
    got = await _value(tools, sid, _CLICK)
    return bool(isinstance(got, dict) and got.get("clicked"))


async def decline_consent(tools: Any, sid: str) -> bool:
    """Decline a cookie banner the page shows: its control that refuses non-essential cookies is clicked (`is_decline`:
    "Reject all", "Decline", "Necessary only"), once, and True is the answer. A page with no banner, or a banner that
    offers only accept, is left as it is (False): nothing is ever accepted, and what the banner hides is read past, not
    waited for. The page is looked at once, with no wait, and a failed look is no banner."""
    try:
        labels = await _value(tools, sid, _CONSENT_FIND)
        if not isinstance(labels, list):
            return False
        for number, label in enumerate(labels):
            if consent.is_decline(label if isinstance(label, str) else None):
                got = await _value(tools, sid, _CONSENT_CLICK.replace("%NUMBER%", str(number)))
                return bool(isinstance(got, dict) and got.get("clicked"))
    except bc.BrowserUnavailable:  # the tab went: the page's own reading reports it
        return False
    return False


def clean_keyword(word: str | None) -> str | None:
    """The word typed in a search box: the owner's own, one line, at most 80 characters, no control characters."""
    word = " ".join("".join(c for c in (word or "") if c.isprintable()).split())[:80]
    return word or None


async def type_word(tools: Any, sid: str, word: str) -> bool:
    got = await _value(tools, sid, _type_script(word))
    return bool(isinstance(got, dict) and got.get("typed"))


async def suggestions(tools: Any, sid: str) -> list[str]:
    """The texts of the entries of the site's own list of suggestions under the marked box, marked in the page; nothing
    is clicked."""
    got = await _value(tools, sid, _SUGGEST_LIST)
    options = got.get("options") if isinstance(got, dict) else None
    return [o for o in options if isinstance(o, str)] if isinstance(options, list) else []


async def pick_option(tools: Any, sid: str, index: int) -> bool:
    got = await _value(tools, sid, _pick_script(index))
    return bool(isinstance(got, dict) and got.get("picked"))


async def asks_for_password(tools: Any, sid: str) -> bool:
    """Whether the page shows a password field, or cannot be looked at: nothing is typed on it."""
    got = await _value(tools, sid, _PASSWORD)
    return not (isinstance(got, dict) and got.get("password") is False)


def vet(step: str, found: dict, page_url: str) -> str | None:
    """Why a found link may not be followed (the reason), or None. Only a link has an address to vet; a button or a
    box does not take the visitor anywhere the page did not already say."""
    href = found.get("href")
    if not href:
        return None
    if not public_url(href):
        return "its address is not a public one"
    if (urlsplit(href).hostname or "") != (urlsplit(page_url).hostname or ""):
        return "it goes to another site"
    return None


async def _search_for(tools: Any, sid: str, keyword: str, new_traffic: Callable[[], list[dict]],
                      settle: Callable[..., Awaitable[None]], host: str) -> int | None:
    """The empty search did nothing: type the one keyword in the same box, submit it, and say a refusal's status."""
    await bc.sleep(ACTION_GAP)
    if await find(tools, sid, "search") is None or not await type_word(tools, sid, keyword):
        return None
    new_traffic()
    if not await click(tools, sid):
        return None
    await settle(ACTION_SETTLE)
    return refused(new_traffic(), host)


def recency_plan(found: dict, today: date) -> dict | None:
    """What to change a recency filter to, from what the page showed of it (`_FIND["recency"]`): {format, before,
    after[, choices]} (`search_fill.clean_since`'s shape), or None when the control is no usable recency filter: a list
    needs two choices whose labels say how far back they reach and one other than the one it has; a number says
    in its label whether it counts days or seconds; a date is set a week back."""
    kind, before = found.get("kind"), found.get("value") if isinstance(found.get("value"), str) else ""
    hint = str(found.get("hint") or "")
    if kind == "select":
        choices = search_fill.choices_of(found.get("options"))
        limited = [c for c in choices if c["seconds"] is not None and c["value"] != before]
        if len(choices) < 2 or not limited:
            return None
        pick = min(limited, key=lambda c: abs(c["seconds"] - RECENCY_DAYS * 86400))
        raw = {"format": "choices", "before": before, "after": pick["value"], "choices": choices}
    elif kind == "date":
        raw = {"format": "date", "before": before, "after": (today - timedelta(days=RECENCY_DAYS)).isoformat()}
    elif kind == "number":
        unit = next((u for u, word in (("days", r"\bdays?\b"), ("seconds", r"\bseconds?\b"))
                     if re.search(word, hint, re.I)), None)
        if unit is None:
            return None
        raw = {"format": unit, "before": before, "after": str(RECENCY_DAYS if unit == "days" else RECENCY_DAYS * 86400)}
    else:
        return None
    if raw["after"] == raw["before"]:
        return None
    return search_fill.clean_since(raw)


async def _typed_search(tools: Any, sid: str, words: list[str], places: list[str], out: Outcome,
                        new_traffic: Callable[[], list[dict]], settle: Callable[..., Awaitable[None]],
                        host: str, today: date | None = None) -> None:
    """The first recording of an added site's search: type the first title in the search box and submit, then the
    second title when there is one, then the places: the first in a plain location box; in a box that shows the
    site's own list of suggestions, each place (at most `search_fill.PLACES_MAX`) is typed and the entry of the list
    that is the place, or starts with it, is picked (`pick_place`), and only that. After each, note which data
    requests the page made (`out.after`). A box the page has none of, a page with a password field, a list with no
    matching entry: that
    typing is skipped, with its reason in `out.skipped`. After them, once, the page's recency filter is changed
    (`recency`). A refusal sets `out.stop` and ends it."""
    typed: dict = {"phrase": None, "second": None, "place": None, "since": None}
    placed: list[str] = []

    async def recency() -> None:
        """Change the site's own recency filter once, to a value other than its own, and note what was asked."""
        found = await find(tools, sid, "recency")
        if found is None:
            out.skipped["since"] = "the page has no recency filter in its search form"
            return
        plan = recency_plan(found, today or datetime.now(UTC).date())
        if plan is None:
            out.skipped["since"] = "its recency filter cannot be read (no two choices that say how far back they reach)"
            return
        if await asks_for_password(tools, sid):
            out.skipped["since"] = "the page asks for a password, so nothing is changed on it"
            return
        here = await snapshot(tools, sid)
        new_traffic()
        if plan["format"] == "choices":
            done = bool((await _value(tools, sid, _choose_script(plan["after"])) or {}).get("set"))
        else:
            done = await type_word(tools, sid, plan["after"])
        if not done:
            out.skipped["since"] = "it could not be changed"
            return
        await settle(ACTION_SETTLE)
        traffic = new_traffic()
        if not any(r.get("kind") in ("XHR", "Fetch", "Document") for r in traffic):  # nothing asked: submit its form
            if not await _value(tools, sid, _SUBMIT):
                out.skipped["since"] = "changing it made the page ask for nothing"
                return
            await settle(ACTION_SETTLE)
            traffic = new_traffic()
        if not any(r.get("kind") in ("XHR", "Fetch", "Document") for r in traffic):
            out.skipped["since"] = "changing it made the page ask for nothing"
            return
        for r in traffic:
            if r.get("request_id") and r.get("kind") in ("XHR", "Fetch"):
                out.after.setdefault(r["request_id"], "since")
        typed["since"] = plan
        out.before_url = here.url if here else None
        out.stop = refused(traffic, host)


    async def pick_place(word: str, tag: str) -> bool:
        """Type the place in the box that offers suggestions and pick the entry of the site's own list that is the
        place (exactly, else starting with it); the form is submitted only when the pick asked the site for nothing."""
        if not await type_word(tools, sid, word):
            out.skipped[tag] = "it could not be typed in"
            return False
        options: list[str] = []
        for i in range(SUGGEST_POLLS):  # the list shows a moment after the typing
            if i:
                await bc.sleep(1.0)
            if options := await suggestions(tools, sid):
                break
        if not options:
            out.skipped[tag] = "its box offers no list of suggestions that shows"
            return False
        index = search_fill.pick_suggestion(options, word)
        if index is None:
            out.skipped[tag] = "no entry of its list of suggestions is the place"
            return False
        new_traffic()  # what the typing asked for (the list itself) is not the search's
        if not await pick_option(tools, sid, index):
            out.skipped[tag] = "the entry could not be picked"
            return False
        await settle(ACTION_SETTLE)
        traffic = new_traffic()
        if not any(r.get("kind") in ("XHR", "Fetch", "Document") for r in traffic):  # the pick asked nothing: submit
            if await find(tools, sid, "place") is None or not await click(tools, sid):
                out.skipped[tag] = "it could not be submitted"
                return False
            await settle(ACTION_SETTLE)
            traffic = new_traffic()
        for r in traffic:
            if r.get("request_id") and r.get("kind") in ("XHR", "Fetch"):
                out.after.setdefault(r["request_id"], tag)
        here = await _value(tools, sid, _HERE)
        if isinstance(here, dict) and isinstance(here.get("url"), str):
            out.place_urls[word] = here["url"]
        out.stop = refused(traffic, host)
        return True

    async def one(step: str, word: str, tag: str) -> bool:
        found = None
        for _ in range(int(FIND_WAIT) + 1):  # a page that builds its form in the browser is given time to
            if (found := await find(tools, sid, step)) is not None:
                break
            await bc.sleep(1.0)
        if found is None:
            out.skipped[tag] = "the page has none"
            return False
        if step == "place" and placed and found.get("plain"):
            return False  # a plain box takes the first place only
        if await asks_for_password(tools, sid):
            out.skipped[tag] = "the page asks for a password, so nothing is typed on it"
            return False
        if step == "place" and not found.get("plain"):
            return await pick_place(word, tag)
        if not await type_word(tools, sid, word):
            out.skipped[tag] = "it could not be typed in"
            return False
        new_traffic()
        if not await click(tools, sid):
            out.skipped[tag] = "it could not be submitted"
            return False
        await settle(ACTION_SETTLE)
        traffic = new_traffic()
        typed[tag] = word
        for r in traffic:
            if r.get("request_id") and r.get("kind") in ("XHR", "Fetch"):
                out.after.setdefault(r["request_id"], tag)
        out.stop = refused(traffic, host)
        return True

    plan = [("search", words[0], "phrase")]
    if len(words) > 1:
        plan.append(("search", words[1], "second"))
    for step, word, tag in plan:
        if typed["phrase"] is None and tag != "phrase":
            out.skipped[tag] = "no title was typed"
            continue
        if tag != "phrase":
            await bc.sleep(ACTION_GAP)
        if await one(step, word, tag) and tag == "phrase":
            out.done.append("search")
        if out.stop is not None:
            break
    for word in places[:search_fill.PLACES_MAX] if out.stop is None else []:
        if typed["phrase"] is None:
            out.skipped["place"] = "no title was typed"
            break
        await bc.sleep(ACTION_GAP)
        tag = search_fill.place_tag(len(placed))
        if await one("place", word, tag):
            placed.append(word)
            out.skipped.pop("place", None)  # an earlier place that found no entry is no reason against the later ones
            typed["place"], typed["more_places"] = placed[0], placed[1:]
        if out.stop is not None or out.skipped.get(tag, "").startswith("the page"):
            break
    if typed["phrase"] and out.stop is None:
        await bc.sleep(ACTION_GAP)
        await recency()
    if typed["phrase"]:
        out.typed = search_fill.clean_typed(typed)


async def work(tools: Any, sid: str, page_url: str, *, new_traffic: Callable[[], list[dict]],
               allowed: Callable[[str], Awaitable[bool]] | None = None, settle: Callable[..., Awaitable[None]],
               steps: tuple[str, ...] = STEPS, keyword: str | None = None, phrases: tuple[str, ...] = (),
               place: str | None = None, more_places: tuple[str, ...] = (), today: date | None = None) -> Outcome:
    """Do the steps on the page the tab is on. `new_traffic()` gives the requests the page made since the last call (to
    look for a refusal), `allowed(url)` says whether robots.txt allows a link, and `settle(seconds)` lets the page's
    data requests finish. `keyword` is one word from the owner's own target roles, typed in the search box only when the
    search with an empty query brought no data request and no new page. `phrases` (the owner's titles, the first two are
    used) and `place` (the owner's first place) are for an added site's first recording: the search is then not run
    empty first but typed (`_typed_search`), and `keyword` is not used; with no title nothing is typed at all.
    `more_places` (the owner's other places) are typed only where the site shows its own list of suggestions to pick
    from."""
    keyword = clean_keyword(keyword)
    words = list(dict.fromkeys(w for w in (clean_keyword(p) for p in phrases) if w))[:2]
    wanted = list(dict.fromkeys(w for w in (clean_keyword(p) for p in (place, *more_places)) if w))
    host = urlsplit(page_url).hostname or ""
    out = Outcome([], {}, [])
    if await decline_consent(tools, sid):  # a cookie banner is no stop and no step: refuse its cookies and go on
        out.declined = True
        new_traffic()  # what the banner's own answer asked for is not a step's
        await settle(ACTION_SETTLE)
    first = await snapshot(tools, sid)
    if first:
        out.snapshots.append(first)
    loop = asyncio.get_running_loop()
    until = loop.time() + INTERACT_SECONDS
    for step in steps:
        if len(out.done) >= ACTIONS:
            out.skipped[step] = "the most actions were done"
            continue
        if loop.time() >= until:
            out.skipped[step] = "the time was up"
            continue
        if out.done:
            await bc.sleep(ACTION_GAP)
        if step == "search" and words:  # an added site's first recording: the owner's own words, typed and looked at
            await _typed_search(tools, sid, words, wanted, out, new_traffic, settle, host, today)
            if out.stop is not None:
                break
            if snap := await snapshot(tools, sid):
                out.snapshots.append(snap)
            if "search" not in out.done:
                out.skipped.setdefault("search", "nothing could be typed")
            continue
        found = None
        for _ in range(int(FIND_WAIT) + 1):
            if (found := await find(tools, sid, step)) is not None or loop.time() >= until:
                break
            await bc.sleep(1.0)
        if found is None:
            out.skipped[step] = "the page has none"
            continue
        current = (out.snapshots[-1].url if out.snapshots else page_url) or page_url
        why = vet(step, found, current)
        if why is None and found.get("href") and allowed is not None and not await allowed(found["href"]):
            why = "robots.txt asks not to read it"
        if why is not None:
            out.skipped[step] = why
            continue
        new_traffic()  # what came before is not this step's
        if not await click(tools, sid):
            out.skipped[step] = "it could not be clicked"
            continue
        out.done.append(step)
        await settle(ACTION_SETTLE)
        traffic = new_traffic()
        if (stop := refused(traffic, host)) is not None:
            out.stop = stop
            break
        if step == "search" and keyword and found.get("what") == "search box" and not any(
                r.get("kind") in ("XHR", "Fetch", "Document") for r in traffic):
            if (stop := await _search_for(tools, sid, keyword, new_traffic, settle, host)) is not None:
                out.stop = stop
                break
        snap = await snapshot(tools, sid)
        if snap:
            out.snapshots.append(snap)
    return out
