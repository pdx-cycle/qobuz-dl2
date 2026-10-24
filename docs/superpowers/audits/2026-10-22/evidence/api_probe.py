# ruff: noqa: E402
"""Synthetic offline audit probe. Observations describe defects, not passing regression tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

import asyncio

import httpx

from qobuz_dl2.bundle import Bundle
from qobuz_dl2.qopy import Client


async def main():
    responses = []
    calls = []

    async def handler(request):
        calls.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def client_for(secrets=()):
        client = Client("synthetic-token", "123456789", secrets)
        await client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    client = await client_for(["candidate1"])
    responses.append(httpx.Response(401, json={}))
    try:
        await client.initialize()
    except Exception as e:
        print("initialize authentication failure:", type(e).__name__, "closed=", client.client.is_closed)
    await client.aclose()

    client = await client_for()
    responses.append(httpx.Response(200, json={}))
    await client.auth()
    print("empty login response accepted:", client.label)
    responses.append(httpx.Response(200, json={"user": {"credential": None}}))
    try:
        await client.auth()
    except Exception as e:
        print("null credential:", type(e).__name__, str(e))
    responses.extend([httpx.Response(400, text="<html>Bad Request</html>"), httpx.Response(200, json={"url": "fake"})])
    client.secrets = ["candidate1", "candidate2"]
    try:
        await client.cfg_setup()
    except Exception as e:
        print("non-JSON secret rejection:", type(e).__name__, "remaining responses=", len(responses))
    responses.clear()
    responses.extend([httpx.Response(503, json={}), httpx.Response(200, json={"id": 1})])
    before = len(calls)
    try:
        await client.get_track_meta(1)
    except Exception as e:
        print("transient 503:", type(e).__name__, "calls=", len(calls) - before, "remaining=", len(responses))
    responses.clear()
    responses.extend([httpx.ReadTimeout("synthetic"), httpx.Response(200, json={"id": 1})])
    before = len(calls)
    try:
        await client.get_track_meta(1)
    except Exception as e:
        print("transient timeout:", type(e).__name__, "calls=", len(calls) - before, "remaining=", len(responses))
    await client.aclose()


asyncio.run(main())
bundle = Bundle.__new__(Bundle)
bundle._bundle = (
    'a.initialSeed("c2VlZHZhbHVl",window.utimezone.berlin);b.initialSeed("c2VlZHZhbHVl",window.utimezone.paris)'
)
print("incomplete bundle accepted:", dict(bundle.get_secrets()))
