"""Measure existing collection scheduling with synthetic delayed downloads."""

# ruff: noqa: E402
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from qobuz_dl2.core import QobuzDL


async def main():
    with tempfile.TemporaryDirectory(prefix="qobuz-scheduling-") as directory:
        q = QobuzDL(directory=directory, download_limit=8, no_m3u_for_playlists=True)

        async def pages(_):
            yield {"name": "Synthetic", "tracks": {"items": [{"id": n} for n in range(8)]}}

        q.client = SimpleNamespace(get_plist_meta=pages, get_artist_meta=pages, get_label_meta=pages)
        active = peak = completed = 0

        async def delayed(*args):
            nonlocal active, peak, completed
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            completed += 1

        q.download_from_id = delayed
        start = time.perf_counter()
        await q.handle_url("https://play.qobuz.com/playlist/1")
        print(f"limit=8 completed={completed} peak_active={peak} elapsed_seconds={time.perf_counter() - start:.3f}")


asyncio.run(main())
