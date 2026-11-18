"""The engine, the program that reads sites, keeps the overlay of the sites the owner added (`sites/added.py`): it is
made as the engine starts, and again at each datagram it hears (`notice.observe`), and the observer is gone when
the engine stops. No network and no database server: the start is a recorder."""

import contextlib

from app import notice
from app.jobsearch import engine
from app.sites import added


async def test_the_engines_start_makes_the_overlay_inside_the_start_and_hears_datagrams_until_its_end(monkeypatch):
    reloaded: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_start(_application):
        yield

    async def reload() -> int:
        reloaded.append("reload")
        return 0

    monkeypatch.setattr(engine, "_start", fake_start)
    monkeypatch.setattr(added, "reload", reload)
    monkeypatch.setattr(notice, "_observers", [])
    async with engine.lifespan(engine.app):
        assert reloaded == ["reload"]
        assert added.heard in notice._observers
    assert notice._observers == []


def test_every_datagram_the_listener_decodes_is_shown_to_the_observers_before_the_receivers_own_handler(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(notice, "_observers", [])
    notice.observe(lambda message: order.append(f"observer:{message.bell}"))
    notice.observe(lambda message: 1 / 0)  # an observer that raises is logged and skipped
    receiver = notice.Receiver(0, lambda message: order.append(f"receiver:{message.bell}"))
    listener = notice._Listener(receiver)
    listener.datagram_received(notice.encode_bell("look", {}, boot="b", n=1), (notice.LOOPBACK, 5))
    listener.datagram_received(b"garbage", (notice.LOOPBACK, 5))
    listener.datagram_received(notice.encode_bell("look", {}, boot="b", n=2), ("10.0.0.5", 5))  # not the loopback
    assert order == ["observer:look", "receiver:look"]


def test_an_observer_is_added_once_and_taken_away_by_name(monkeypatch):
    monkeypatch.setattr(notice, "_observers", [])
    notice.observe(added.heard)
    notice.observe(added.heard)
    assert notice._observers == [added.heard]
    notice.unobserve(added.heard)
    notice.unobserve(added.heard)
    assert notice._observers == []
