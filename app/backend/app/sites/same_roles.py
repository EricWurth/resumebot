"""Whether a learned search reads the roles the site showed (docs/PRD.md path 37.17). Pure: no network, no database.

**Offline.** A recording that holds the page's visible result cards (`cards`, read from the rendered page by code) and
the data answer the page asked for is a proof against itself: the recipe, replayed on that answer, must read the roles
the cards show, by the board's own id or by title (and company, when both sides say one). It must find at least
`SHARE_MIN` of the cards, else it is not kept and the chain goes on. A recording with fewer than `CARDS_MIN` cards says
too little to judge by, and the check is skipped.

**Live.** On the first read after an add, a second title (when the owner has two) must return roles too, and not the
same list as the first (`same_list`): a search that ignores the words returns one list whatever it is asked.

Nothing here names a site: the kind of the data and what it holds decide."""

import re
from typing import Any

#: The most cards a recording keeps, and the fewest that make the check worth making.
CARDS_MAX = 60
CARDS_MIN = 3
#: The share of the page's cards the recipe's roles must hold (the tolerance: the page may show a sort, a promoted
#: role or a page the answer does not carry).
SHARE_MIN = 0.7
#: What the offline problem starts with, so a caller can tell it from the others.
PROBLEM = "the recipe does not read the roles the page showed"

_WORDS = re.compile(r"[^a-z0-9]+")


def _norm(value: Any) -> str:
    return _WORDS.sub(" ", str(value or "").lower()).strip()


def clean_cards(raw: Any) -> list[dict]:
    """The cards a recording reports, as {id, title, company} with words of their own (a title at least), bounded."""
    out: list[dict] = []
    for card in raw if isinstance(raw, list) else []:
        if not isinstance(card, dict) or not _norm(card.get("title")):
            continue
        out.append({"id": str(card["id"])[:120] if card.get("id") else None,
                    "title": str(card["title"])[:200], "company": str(card["company"])[:120] if card.get("company")
                    else None})
        if len(out) >= CARDS_MAX:
            break
    return out


def _key(posting: dict) -> str:
    """A role's identity for comparing two lists: the board's id when it has one, else title and company."""
    ident = posting.get("ext_id")
    return f"id:{ident}" if ident else f"t:{_norm(posting.get('title'))}|{_norm(posting.get('company'))}"


def same_list(first: list[dict], second: list[dict]) -> bool:
    """Whether two reads returned the same roles (as a set: a different order is the same list)."""
    return {_key(p) for p in first} == {_key(p) for p in second}


def _found(card: dict, postings: list[dict]) -> bool:
    ident, title, company = card.get("id"), _norm(card.get("title")), _norm(card.get("company"))
    for p in postings:
        if ident and (str(p.get("ext_id") or "") == str(ident) or str(ident) in str(p.get("url") or "")):
            return True
        if title and _norm(p.get("title")) == title and (
                not company or not _norm(p.get("company")) or _norm(p.get("company")) == company):
            return True
    return False


def problems(postings: list[dict], cards: Any) -> list[str]:
    """What is wrong with the roles a recipe read against the cards the page showed: [] when the check holds or does
    not apply (too few cards)."""
    cards = clean_cards(cards)
    if len(cards) < CARDS_MIN:
        return []
    found = sum(1 for c in cards if _found(c, postings))
    if found >= SHARE_MIN * len(cards):
        return []
    return [f"{PROBLEM}: {found} of the {len(cards)} roles in its cards, at least {SHARE_MIN:.0%} are needed"]
