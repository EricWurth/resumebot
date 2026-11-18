"""The address a typed search leaves in the address bar, kept by the recording as a template for the page's own HTML
(`recipe_record.capture_page`, `recipe_record.record_by_code`). The browser is scripted: no network, no Chrome, a
made-up site on `example.test`."""

import json

from app.jobsearch.browser.recipes import record as recipe_record
from tests.jobsearch.test_recipe_interact import PAGE, quick  # noqa: F401  (a fixture, used by name)
from tests.jobsearch.test_recipe_typing import SEARCH, TypingPage, _rules


class AddressPage(TypingPage):
    """A tab whose address, after a search, holds what its two boxes hold (a plain GET form)."""

    async def call(self, method, params=None, session_id=None):
        expression = (params or {}).get("expression", "")
        if method == "Runtime.evaluate" and "outerHTML" in expression:
            here = f"{PAGE}?q={self.values.get('search', '')}&where={self.values.get('place', '')}" if self.values \
                else PAGE
            return {"result": {"value": json.dumps({"url": here, "html": self.html})}}
        return await super().call(method, params, session_id)


async def test_the_address_bar_after_the_typed_search_is_kept_as_a_template_with_blanks_where_the_words_were():
    raw, stop = await recipe_record.capture_page(PAGE, AddressPage(_rules()), search=SEARCH)
    assert stop is None
    assert {"search_url": f"{PAGE}?q={{phrase}}&where={{place}}"} in raw
    assert "Chef" not in json.dumps([r for r in raw if "search_url" in r]), "the template holds none of the words"


async def test_a_page_whose_address_does_not_change_with_the_search_makes_no_template():
    raw, _stop = await recipe_record.capture_page(PAGE, TypingPage(_rules()), search=SEARCH)
    assert not any("search_url" in r for r in raw)


async def test_with_no_search_given_there_is_no_template():
    raw, _stop = await recipe_record.capture_page(PAGE, AddressPage(_rules()))
    assert not any("search_url" in r or "typed" in r for r in raw)
