import asyncio
import importlib.util
from datetime import datetime, timezone

import httpx
import pytest
from tenacity import wait_none


def network():
    assert importlib.util.find_spec("qobuz_dl2.network") is not None, "Classified retries missing"
    from qobuz_dl2 import network

    return network


@pytest.mark.parametrize("status,expected", [(503, 2), (429, 2), (400, 1), (401, 1), (403, 1), (404, 1)])
def test_get_retries_only_transient_statuses(status, expected):
    n = network()

    async def run():
        calls = []

        def reply(req):
            calls.append(req)
            return httpx.Response(status if len(calls) == 1 else 200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as c:
            if expected == 1:
                with pytest.raises(httpx.HTTPStatusError):
                    await n.get_with_retry.retry_with(wait=wait_none())(c, "https://example.test/api")
            else:
                r = await n.get_with_retry.retry_with(wait=wait_none())(c, "https://example.test/api")
                assert r.json() == {"ok": True}
        assert len(calls) == expected

    asyncio.run(run())


def test_retry_after_seconds_and_date_are_bounded():
    n = network()
    request = httpx.Request("GET", "https://example.test")
    for value, want in [("4", 4), ("9999", 60), ("Thu, 22 Oct 2026 12:00:10 GMT", 10)]:
        r = httpx.Response(429, request=request, headers={"Retry-After": value})
        error = httpx.HTTPStatusError("limited", request=request, response=r)
        assert n.retry_delay(error, 1, datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)) == want


def test_retry_exhaustion_preserves_type_and_budget():
    n = network()

    async def run():
        calls = []

        def response(req):
            calls.append(req)
            raise httpx.ReadTimeout("synthetic")

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as c:
            with pytest.raises(httpx.ReadTimeout):
                await n.get_with_retry.retry_with(wait=wait_none())(c, "https://example.test/api")
        assert len(calls) == 3

    asyncio.run(run())


def test_cancel_during_retry_wait_prevents_more_requests():
    n = network()

    async def run():
        requested = asyncio.Event()
        calls = []

        def response(req):
            calls.append(req)
            requested.set()
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as c:
            task = asyncio.create_task(
                n.get_with_retry.retry_with(wait=lambda state: 30)(c, "https://example.test/api")
            )
            await requested.wait()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == 1

    asyncio.run(run())
