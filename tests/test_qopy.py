import asyncio

import pytest

from qobuz_dl2.qopy import Client


@pytest.mark.parametrize("getter", ["get_artist_meta", "get_plist_meta", "get_label_meta"])
def test_collection_getters_return_async_iterators(getter):
    async def run():
        c = Client("synthetic", "123", [])

        async def api(*args, **kwargs):
            return {
                "name": "Artist",
                "albums_count": 1,
                "tracks_count": 1,
                "albums": {"items": [{"id": "1"}]},
                "tracks": {"items": [{"id": "1"}]},
            }

        c.api_call = api
        try:
            pages = getattr(c, getter)("1")
            assert hasattr(pages, "__aiter__"), "Collection getter returned coroutine"
            assert [p async for p in pages] == [await api()]
        finally:
            await c.aclose()

    asyncio.run(run())


def test_failed_initialize_closes_client():
    async def run():
        c = Client("synthetic", "123", ["one"])

        async def fail():
            from qobuz_dl2.exceptions import AuthenticationError

            raise AuthenticationError("synthetic")

        c.auth = fail
        with pytest.raises(Exception):
            await c.initialize()
        assert c.client.is_closed
        await c.aclose()

    asyncio.run(run())


def test_initialize_does_not_probe_streaming_after_successful_authentication():
    import httpx

    async def run():
        requests = []

        def reply(request):
            requests.append(request)
            if request.url.path.endswith("/user/login"):
                return httpx.Response(
                    200,
                    json={
                        "user_auth_token": "refreshed",
                        "user": {
                            "id": 42,
                            "credential": {"parameters": {"short_label": "Studio"}},
                        },
                    },
                )
            return httpx.Response(400, json={"message": "stream signature rejected"})

        c = Client("synthetic", "123", ["candidate"])
        await c.aclose()
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        try:
            await c.initialize()
            assert c.label == "Studio"
            assert [request.url.path for request in requests] == ["/api.json/0.2/user/login"]
        finally:
            await c.aclose()

    asyncio.run(run())


def test_track_url_selects_stream_secret_lazily():
    from qobuz_dl2.exceptions import InvalidAppSecretError

    async def run():
        c = Client("synthetic", "123", ["candidate"])
        calls = []

        async def api_call(endpoint, **kwargs):
            calls.append((endpoint, kwargs.copy()))
            if kwargs.get("sec") == "candidate":
                return {"url": "https://example.test/probe"}
            if c.sec == "candidate":
                return {"url": "https://example.test/track"}
            raise InvalidAppSecretError("stream secret not selected")

        c.api_call = api_call
        try:
            result = await c.get_track_url("99", 6)
            assert result == {"url": "https://example.test/track"}
            assert c.sec == "candidate"
            assert calls == [
                ("track/getFileUrl", {"id": 5966783, "fmt_id": 5, "sec": "candidate"}),
                ("track/getFileUrl", {"id": "99", "fmt_id": 6}),
            ]
        finally:
            await c.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "body", [{}, [], {"user": {"id": 1, "credential": None}}, {"user": {"id": 1}, "user_auth_token": 123}]
)
def test_malformed_auth_is_typed_and_does_not_persist(body):
    import httpx

    from qobuz_dl2.exceptions import AuthenticationError

    async def run():
        saved = []
        c = Client("synthetic", "123", [], on_token_refresh=saved.append)
        await c.aclose()
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
        try:
            with pytest.raises(AuthenticationError):
                await c.auth()
            assert not saved
        finally:
            await c.aclose()

    asyncio.run(run())


def test_nonjson_secret_rejection_tries_next_candidate():
    import httpx

    async def run():
        c = Client("synthetic", "123", ["bad", "good"])
        await c.aclose()
        calls = []

        def reply(req):
            calls.append(req)
            return (
                httpx.Response(400, text="<html>bad</html>")
                if len(calls) == 1
                else httpx.Response(200, json={"url": "https://example.test/audio"})
            )

        c.client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        try:
            await c.cfg_setup()
            assert c.sec == "good"
            assert len(calls) == 2
        finally:
            await c.aclose()

    asyncio.run(run())


def test_retired_stream_endpoint_is_not_reported_as_invalid_secret():
    import httpx

    from qobuz_dl2.exceptions import StreamingEndpointRetiredError

    async def run():
        c = Client("synthetic", "123", ["candidate-one", "candidate-two"])
        await c.aclose()
        calls = []

        def reply(request):
            calls.append(request)
            return httpx.Response(410, json={"message": "Gone"})

        c.client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        try:
            with pytest.raises(StreamingEndpointRetiredError, match="authentication succeeded"):
                await c.cfg_setup()
            assert len(calls) == 1
        finally:
            await c.aclose()

    asyncio.run(run())


def test_rejected_stream_secrets_do_not_blame_a_valid_login():
    import httpx

    from qobuz_dl2.exceptions import InvalidAppSecretError

    async def run():
        c = Client("synthetic", "123", ["candidate-one", "candidate-two"])
        await c.aclose()
        c.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"message": "stream signature rejected"})
            )
        )
        try:
            with pytest.raises(InvalidAppSecretError, match="Authentication succeeded") as caught:
                await c.cfg_setup()
            assert "reset" not in str(caught.value).lower()
            assert "token remains valid" in str(caught.value).lower()
        finally:
            await c.aclose()

    asyncio.run(run())
