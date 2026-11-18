"""Typing the owner's own words while an added site's first recording is made (`recipe_interact.work` with `phrases`
and `place`, and `recipe_record.capture_page` with `search`). The browser is scripted: no network, no Chrome, a made-up
site on `example.test`."""

import json

from app.jobsearch.browser.recipes import interact as recipe_interact
from app.jobsearch.browser.recipes import record as recipe_record
from app.sites import recipes, search_fill
from tests.jobsearch.test_recipe_interact import ALL, API, PAGE, Page, _jobs, quick  # noqa: F401

PLACE_BOX = {"found": True, "what": "place box", "href": None, "plain": True}
SEARCH = search_fill.Search(("Chef de Partie", "Sous Chef"), ("Northtown", "Remote"))


class TypingPage(Page):
    """A tab whose boxes hold what is typed in them: a search box and, when the page has one, a location box. Each
    submit makes the data request the page's form would, from the words in its boxes."""

    def __init__(self, finds, *, password: bool = False, answer_status: int = 200, **kw) -> None:
        super().__init__(finds, **kw)
        self.password, self.answer_status = password, answer_status
        self.words: list[tuple[str, str]] = []  # (the step whose box was typed in, the word)
        self.values: dict[str, str] = {}

    async def call(self, method, params=None, session_id=None):
        e = (params or {}).get("expression", "")
        if method == "Runtime.evaluate" and "password:" in e:
            return {"result": {"value": json.dumps({"password": self.password})}}
        if method == "Runtime.evaluate" and "'place box'" in e:
            found = self.finds.get("place")
            self.last = "place" if found else self.last
            return {"result": {"value": json.dumps(found or {"found": False})}}
        if method == "Runtime.evaluate" and "typed:" in e:
            self.typed.append(e)
            box = self.last or ""
            word = json.loads(e.split("call(el, ", 1)[1].split("); for", 1)[0])
            if not self.finds.get(box):
                return {"result": {"value": json.dumps({"typed": False})}}
            self.values[box] = word
            self.words.append((box, word))
            return {"result": {"value": json.dumps({"typed": True})}}
        if method == "Runtime.evaluate" and "clicked:" in e and self.last in ("search", "place"):
            self.clicks.append(self.last)
            q = f"?q={self.values.get('search', '')}&where={self.values.get('place', '')}"
            self.request("XHR", f"{API}{q}", self.answer_status, "application/json", json.dumps({"results": _jobs(3)}))
            return {"result": {"value": json.dumps({"clicked": True})}}
        return await super().call(method, params, session_id)


async def _settle(_seconds: float) -> None:
    return None


async def _work(page, **kw):
    return await recipe_interact.work(page, "s", PAGE, new_traffic=lambda: recipe_record.assemble(page.events),
                                      settle=_settle, **kw)


def _rules(**over):
    return {**ALL, "search": {"found": True, "what": "search box", "href": None}, "place": PLACE_BOX, **over}


async def test_the_first_title_the_second_title_and_the_place_are_typed_once_each_in_vetted_boxes():
    page = TypingPage(_rules())
    out = await _work(page, phrases=SEARCH.phrases, place="Northtown")
    assert page.words == [("search", "Chef de Partie"), ("search", "Sous Chef"), ("place", "Northtown")]
    assert out.typed == {"phrase": "Chef de Partie", "second": "Sous Chef", "place": "Northtown"}
    assert out.done[0] == "search" and out.stop is None
    assert page.clicks[:3] == ["search", "search", "place"]  # no empty search first, no click but the submitting one


async def test_a_third_title_is_never_typed():
    page = TypingPage(_rules())
    await _work(page, phrases=("A one", "B two", "C three"), place="Northtown")
    assert [w for _, w in page.words] == ["A one", "B two", "Northtown"]


async def test_with_one_title_only_it_is_typed_and_no_second_search_is_made():
    page = TypingPage(_rules())
    out = await _work(page, phrases=("Chef",))
    assert page.words == [("search", "Chef")]
    assert out.typed == {"phrase": "Chef", "second": None, "place": None}


async def test_with_no_titles_nothing_is_typed_and_no_template_can_be_made():
    page = TypingPage(_rules())
    out = await _work(page, phrases=(), place="Northtown")
    assert page.words == [] and out.typed is None and out.after == {}


async def test_a_page_with_a_password_field_is_never_typed_on():
    page = TypingPage(_rules(), password=True)
    out = await _work(page, phrases=SEARCH.phrases, place="Northtown")
    assert page.words == [] and out.typed is None
    assert "password" in out.skipped["phrase"]


async def test_a_page_that_cannot_be_looked_at_for_a_password_is_not_typed_on():
    page = Page(_rules())  # answers nothing to the look
    out = await _work(page, phrases=SEARCH.phrases)
    assert page.typed == [] and out.typed is None


async def test_a_location_box_that_offers_suggestions_but_shows_no_list_is_not_used_and_nothing_is_clicked():
    """Changed 2026-11-17 (PRD 37.16): the place is now typed in such a box, to see the site's own list of suggestions;
    with no list there is no pick and no click, and the place stays unsupported as before."""
    page = TypingPage(_rules(place={**PLACE_BOX, "plain": False}))
    out = await _work(page, phrases=("Chef",), place="Northtown")
    assert page.words == [("search", "Chef"), ("place", "Northtown")] and out.typed["place"] is None
    assert "no list of suggestions" in out.skipped["place"] and "place" not in page.clicks


async def test_a_page_with_no_location_box_gets_the_titles_only():
    page = TypingPage(_rules(place=None))
    out = await _work(page, phrases=SEARCH.phrases, place="Northtown")
    assert out.typed == {"phrase": "Chef de Partie", "second": "Sous Chef", "place": None}


async def test_each_data_request_is_marked_with_the_typing_it_followed():
    page = TypingPage(_rules())
    out = await _work(page, phrases=SEARCH.phrases, place="Northtown")
    assert list(out.after.values()) == ["phrase", "second", "place"]


async def test_a_refusal_after_a_typed_search_stops_the_recording():
    page = TypingPage(_rules())
    original = page.request

    def refuse(kind, url, status, mime, body=None):
        original("Document" if "q=" in url else kind, url, 403 if "q=" in url else status, mime, body)

    page.request = refuse
    out = await _work(page, phrases=SEARCH.phrases, place="Northtown")
    assert out.stop == 403 and page.words == [("search", "Chef de Partie")] and "next" not in out.done
    raw, stop = await recipe_record.capture_page(PAGE, page, search=SEARCH)
    assert (raw, stop) == ([], 403)


async def test_capture_page_returns_the_typed_words_and_marks_the_requests_for_the_recipe_to_be_made_from():
    page = TypingPage(_rules())
    raw, stop = await recipe_record.capture_page(PAGE, page, search=SEARCH)
    assert stop is None
    assert [r["after"] for r in raw if "after" in r] == ["phrase", "second", "place"]
    assert {"typed": {"phrase": "Chef de Partie", "second": "Sous Chef", "place": "Northtown"}} in raw
    capture = recipes.sanitize(raw, PAGE)
    assert capture["typed"] == {"phrase": "Chef de Partie", "second": "Sous Chef", "place": "Northtown"}
    assert [r["after"] for r in capture["responses"]] == ["phrase", "second", "place"]


async def test_the_typed_words_survive_packing_and_the_writer_is_shown_the_typing_each_request_followed():
    page = TypingPage(_rules())
    raw, _ = await recipe_record.capture_page(PAGE, page, search=SEARCH)
    capture = recipes.sanitize(raw, PAGE)
    back = recipes.unpack(recipes.pack(capture))
    assert back["typed"] == capture["typed"]
    text = recipes.trim(back)
    assert '{"typed": ' in text and '"search_typed": "second"' in text
    assert '{"typed": ' not in (recipes.strip_typed(text) or "")


async def test_the_two_typed_searches_show_code_which_field_holds_the_phrase_and_the_place():
    page = TypingPage(_rules())
    raw, _ = await recipe_record.capture_page(PAGE, page, search=SEARCH)
    capture = recipes.sanitize(raw, PAGE)
    by = {r["after"]: search_fill.leaves(r["url"]) for r in capture["responses"]}
    where = search_fill.blank_places(by["phrase"], by["second"], by["place"], capture["typed"])
    assert where == {"phrase": {("q", "q")}, "place": {("q", "where")}}


async def test_a_capture_with_no_search_given_types_nothing_and_keeps_the_empty_search_first():
    page = TypingPage(_rules())
    await recipe_record.capture_page(PAGE, page)
    assert page.words == []
