import asyncio

from ops.notify import Notifier


class Recorder:
    def __init__(self, fail: bool = False):
        self.sent, self.fail = [], fail

    async def __call__(self, url, data, headers):
        if self.fail:
            raise OSError("network down")
        self.sent.append((url, data.decode(), headers))


def test_push_has_priority_and_title():
    post = Recorder()
    n = Notifier("sf-topic", "https://ntfy.example/", token="tk", post=post)
    assert asyncio.run(n.send("urgent", "gave up on hedger"))
    url, body, headers = post.sent[0]
    assert url == "https://ntfy.example/sf-topic" and body == "gave up on hedger"
    assert headers["Priority"] == "5" and headers["Title"] == "QTE SF urgent"
    assert headers["Authorization"] == "Bearer tk"


def test_same_alert_is_pushed_once_per_window():
    post = Recorder()
    n = Notifier("t", post=post)

    async def go():
        await n.send("serious", "quoter looked stuck", key="stuck-quoter")
        await n.send("serious", "quoter looked stuck again", key="stuck-quoter")
        await n.send("serious", "picker looked stuck", key="stuck-picker")

    asyncio.run(go())
    assert [body for _, body, _ in post.sent] == ["quoter looked stuck", "picker looked stuck"]


def test_no_topic_only_logs_and_failures_never_raise():
    post = Recorder()
    assert not asyncio.run(Notifier(None, post=post).send("urgent", "x"))
    assert post.sent == []
    assert not asyncio.run(Notifier("t", post=Recorder(fail=True)).send("urgent", "x"))
