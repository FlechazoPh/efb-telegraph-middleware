import types

import pytest

pytest.importorskip("ehforwarderbot")

from ehforwarderbot import Message, MsgType, coordinator  # noqa: E402
from ehforwarderbot.message import LinkAttribute  # noqa: E402

import efb_telegraph_middleware as mw  # noqa: E402


class FakeArchiver:
    def __init__(self):
        self.calls = []

    def should_archive(self, url):
        return "skip.me" not in url

    def archive(self, url):
        self.calls.append(url)
        return None if "fail" in url else "https://telegra.ph/" + url.rsplit("/", 1)[-1]


@pytest.fixture
def middleware(monkeypatch, tmp_path):
    monkeypatch.setattr(mw, "get_config_path", lambda mid: tmp_path / "none.yaml")
    monkeypatch.setattr(mw, "get_data_path", lambda mid: tmp_path)
    master = object()
    monkeypatch.setattr(coordinator, "master", master, raising=False)
    m = mw.TelegraphMiddleware()
    m.archiver = FakeArchiver()
    return m, master


def test_link_message_gets_telegraph_preview(middleware):
    m, master = middleware
    msg = Message(type=MsgType.Link, deliver_to=master,
                  attributes=LinkAttribute(title="t", description="d", image="https://thumb", url="https://a.com/p1"))
    out = m.process_message(msg)
    assert out is msg and msg.attributes.image == "https://telegra.ph/p1"


def test_text_urls_appended(middleware):
    m, master = middleware
    msg = Message(type=MsgType.Text, deliver_to=master,
                  text="看 https://a.com/one 和 https://skip.me/x 以及 https://b.com/fail")
    m.process_message(msg)
    assert msg.text.endswith("\n\n📰 https://telegra.ph/one")
    assert m.archiver.calls == ["https://a.com/one", "https://b.com/fail"]


def test_ignores_messages_to_slave_and_edits(middleware):
    m, master = middleware
    to_slave = Message(type=MsgType.Text, deliver_to=types.SimpleNamespace(), text="https://a.com/x")
    m.process_message(to_slave)
    edited = Message(type=MsgType.Text, deliver_to=master, text="https://a.com/x", edit=True)
    m.process_message(edited)
    assert m.archiver.calls == []


def test_errors_never_drop_messages(middleware):
    m, master = middleware
    m.archiver.archive = lambda url: (_ for _ in ()).throw(RuntimeError("boom"))
    msg = Message(type=MsgType.Text, deliver_to=master, text="https://a.com/x")
    assert m.process_message(msg) is msg and msg.text == "https://a.com/x"
