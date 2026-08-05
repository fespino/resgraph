"""The API's lazy store init under threadpool concurrency (#80):
two concurrent first requests must share one driver, same contract
the MCP server's init lock guarantees for parallel tool calls."""

import threading
import time
from types import SimpleNamespace

from resgraph.api import app as api_app


class FakeDriver:
    def session(self):
        return SimpleNamespace(close=lambda: None)


def test_concurrent_first_requests_create_one_driver(monkeypatch):
    created = []

    def slow_get_driver():
        time.sleep(0.05)
        created.append(object())
        return FakeDriver()

    monkeypatch.setattr(api_app.hot_client, "get_driver", slow_get_driver)
    state = SimpleNamespace(driver=None, catalog=None)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    barrier = threading.Barrier(8)

    def first_request():
        barrier.wait()
        gen = api_app.get_ctx(request)
        ctx = next(gen)
        ctx.require("hot")
        gen.close()

    threads = [threading.Thread(target=first_request) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1
    assert state.driver is not None
