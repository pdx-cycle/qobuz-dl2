# ruff: noqa: E402
"""Synthetic offline audit probe. Observations describe defects, not passing regression tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from qobuz_dl2.downloader import QL_DOWNGRADE, Download


async def main():
    with tempfile.TemporaryDirectory(prefix="qobuz-policy-", dir="/private/tmp") as folder:

        async def get_track_url(*args, **kwargs):
            return {
                "url": "https://example.test/track",
                "sampling_rate": 44.1,
                "bit_depth": 16,
                "restrictions": [{"code": QL_DOWNGRADE}],
            }

        d = Download(SimpleNamespace(get_track_url=get_track_url), "album", folder, 27, downgrade_quality=False)
        called = []

        async def record(*args, **kwargs):
            called.append("downloaded")

        d._download_and_tag = record
        await d._process_track(None, folder, 0, {"id": "low-quality", "media_number": 1}, {}, False)
        print("per_track_no_fallback_downgraded_calls", called)


asyncio.run(main())
