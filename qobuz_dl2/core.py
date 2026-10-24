import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup as bso
from pathvalidate import sanitize_filename

from qobuz_dl2 import downloader, qopy
from qobuz_dl2.bundle import Bundle
from qobuz_dl2.color import CYAN, OFF, RED, RESET, YELLOW
from qobuz_dl2.db import create_db, record_artifacts
from qobuz_dl2.exceptions import NonStreamable
from qobuz_dl2.results import BatchResult, DownloadResult, DownloadStatus
from qobuz_dl2.scheduling import bounded_map, run_owned_thread
from qobuz_dl2.utils import (
    PartialFormatter,
    create_and_return_dir,
    format_duration,
    get_url_info,
    make_m3u,
    smart_discography_filter,
)

WEB_URL = "https://play.qobuz.com/"
ARTISTS_SELECTOR = "td.chartlist-artist > a"
TITLE_SELECTOR = "td.chartlist-name > a"
QUALITIES = {
    5: "5 - MP3",
    6: "6 - 16 bit, 44.1kHz",
    7: "7 - 24 bit, <96kHz",
    27: "27 - 24 bit, >96kHz",
}

logger = logging.getLogger(__name__)


class QobuzDL:
    def __init__(
        self,
        directory="Qobuz Downloads",
        quality=6,
        embed_art=False,
        lucky_limit=1,
        lucky_type="album",
        interactive_limit=20,
        ignore_singles_eps=False,
        no_m3u_for_playlists=False,
        quality_fallback=True,
        cover_og_quality=False,
        no_cover=False,
        downloads_db=None,
        folder_format="{artist} - {album} ({year}) [{bit_depth}B-{sampling_rate}kHz]",
        track_format="{tracknumber}. {tracktitle}",
        smart_discography=False,
        download_limit=downloader.DEFAULT_DOWNLOAD_LIMIT,
    ):
        quality = int(quality)
        download_limit = int(download_limit)
        if quality not in QUALITIES or download_limit < 1:
            raise ValueError("Unsupported quality or nonpositive download limit")
        self.directory = create_and_return_dir(os.path.expanduser(directory))
        self.quality = quality
        self.embed_art = embed_art
        self.lucky_limit = lucky_limit
        self.lucky_type = lucky_type
        self.interactive_limit = interactive_limit
        self.ignore_singles_eps = ignore_singles_eps
        self.no_m3u_for_playlists = no_m3u_for_playlists
        self.quality_fallback = quality_fallback
        self.cover_og_quality = cover_og_quality
        self.no_cover = no_cover
        self.downloads_db = create_db(downloads_db) if downloads_db else None
        self.folder_format = folder_format
        self.track_format = track_format
        self.smart_discography = smart_discography
        self.download_limit = download_limit
        self.last_result = BatchResult()
        self._text_files = set()
        self._download_client = None
        self._transfer_budget = None
        self._inflight = {}
        self._waiters = {}

    async def initialize_client(self, token, app_id, secrets, on_token_refresh=None):
        self.client = qopy.Client(
            token, app_id, secrets, on_token_refresh=on_token_refresh, request_limit=self.download_limit
        )
        await self.client.initialize()
        logger.info(f"{YELLOW}Set max quality: {QUALITIES[int(self.quality)]}\n")

    async def aclose(self):
        """Close the underlying HTTP client, if one was created."""
        pending = list(self._inflight.values())
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        if self._download_client is not None:
            await self._download_client.aclose()
            self._download_client = None
        self._transfer_budget = None
        client = getattr(self, "client", None)
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    def get_tokens(self):
        bundle = Bundle()
        self.app_id = bundle.get_app_id()
        self.secrets = [secret for secret in bundle.get_secrets().values() if secret]  # avoid empty fields

    async def download_from_id(self, item_id, album=True, alt_path=None):
        key = (
            str(item_id),
            bool(album),
            os.path.realpath(alt_path or self.directory),
            int(self.quality),
            self.embed_art,
            self.no_cover,
            self.folder_format,
            self.track_format,
            self.quality_fallback,
        )
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._download_from_id(item_id, album, alt_path))
            self._inflight[key] = task
            self._waiters[key] = 0
        self._waiters[key] += 1
        try:
            return await asyncio.shield(task)
        finally:
            self._waiters[key] -= 1
            if self._waiters[key] == 0:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._inflight.pop(key, None)
                self._waiters.pop(key, None)

    async def _download_from_id(self, item_id, album=True, alt_path=None):
        finalized = ()
        try:
            if self._download_client is None:
                limit = max(4, int(self.download_limit) + 2)
                self._download_client = httpx.AsyncClient(
                    timeout=downloader.DOWNLOAD_TIMEOUT,
                    limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit),
                )
            if self._transfer_budget is None:
                self._transfer_budget = asyncio.Semaphore(self.download_limit)
            dloader = downloader.Download(
                self.client,
                item_id,
                alt_path or self.directory,
                int(self.quality),
                self.embed_art,
                self.ignore_singles_eps,
                self.quality_fallback,
                self.cover_og_quality,
                self.no_cover,
                self.folder_format,
                self.track_format,
                self.download_limit,
                http_client=self._download_client,
                transfer_budget=self._transfer_budget,
                history_path=self.downloads_db,
            )
            result = await dloader.download_id_by_type(not album)
            finalized = result.artifacts
            await run_owned_thread(record_artifacts, self.downloads_db, result.artifacts)
            return result
        except (
            httpx.HTTPError,
            NonStreamable,
            downloader.DownloadError,
            ConnectionError,
            KeyError,
            ValueError,
            TypeError,
            OSError,
            sqlite3.Error,
        ) as e:
            partial = tuple(getattr(e, "artifacts", ()))
            if partial:
                try:
                    await run_owned_thread(record_artifacts, self.downloads_db, partial)
                except (sqlite3.Error, OSError, ValueError):
                    logger.error("Could not persist partial release history")
            logger.error("Release failed (%s)", type(e).__name__)
            return DownloadResult(DownloadStatus.FAILED, partial or finalized, errors=(type(e).__name__,))

    async def handle_url(self, url):
        try:
            return await self._handle_url(url)
        except Exception as error:
            logger.error("Collection failed (%s)", type(error).__name__)
            return BatchResult((DownloadResult(DownloadStatus.FAILED, errors=(type(error).__name__,)),))

    async def _handle_url(self, url):
        possibles = {
            "playlist": {
                "func": self.client.get_plist_meta,
                "iterable_key": "tracks",
            },
            "artist": {
                "func": self.client.get_artist_meta,
                "iterable_key": "albums",
            },
            "label": {
                "func": self.client.get_label_meta,
                "iterable_key": "albums",
            },
            "album": {"album": True, "func": None, "iterable_key": None},
            "track": {"album": False, "func": None, "iterable_key": None},
        }
        try:
            url_type, item_id = get_url_info(url)
            type_dict = possibles[url_type]
        except (KeyError, IndexError, ValueError):
            logger.info(f'{RED}Invalid url: "{url}". Use urls from https://play.qobuz.com!')
            return BatchResult((DownloadResult(DownloadStatus.FAILED, reason="Invalid URL"),))

        if type_dict["func"]:
            pages = type_dict["func"](item_id)
            try:
                first = await pages.__anext__()
            except StopAsyncIteration:
                return BatchResult((DownloadResult(DownloadStatus.SKIPPED, reason="Empty collection"),))
            name = first.get("name") or f"{url_type} {item_id}"
            new_path = create_and_return_dir(os.path.join(self.directory, sanitize_filename(name)))
            if self.smart_discography and url_type == "artist":
                contents = [first] + [page async for page in pages]
                items = smart_discography_filter(contents)
            else:

                async def collection_items():
                    seen = set()

                    async def members(page):
                        for item in (page.get(type_dict["iterable_key"]) or {}).get("items", []):
                            identity = str(item["id"])
                            if url_type != "playlist":
                                if identity in seen:
                                    continue
                                seen.add(identity)
                            yield item

                    async for item in members(first):
                        yield item
                    async for page in pages:
                        async for item in members(page):
                            yield item

                items = collection_items()

            async def download(item):
                return await self.download_from_id(item["id"], type_dict["iterable_key"] == "albums", new_path)

            outcomes = await bounded_map(download, items, self.download_limit)
            results = tuple(
                r
                if isinstance(r, DownloadResult)
                else DownloadResult(DownloadStatus.FAILED, errors=(type(r).__name__,))
                for r in outcomes
            )
            batch = BatchResult(results or (DownloadResult(DownloadStatus.SKIPPED, reason="Empty collection"),))
            if url_type == "playlist" and not self.no_m3u_for_playlists:
                await run_owned_thread(make_m3u, new_path, batch.artifacts)
            return batch
        return BatchResult((await self.download_from_id(item_id, type_dict["album"]),))

    async def download_list_of_urls(self, urls):
        if not urls or not isinstance(urls, list):
            logger.info(f"{OFF}Nothing to download")
            return BatchResult()
        results = []
        for url in urls:
            try:
                hostname = urlsplit(url).hostname
            except ValueError:
                results.append(DownloadResult(DownloadStatus.FAILED, reason="Malformed URL"))
                continue
            if hostname in {"last.fm", "www.last.fm"}:
                result = await self.download_lastfm_pl(url)
            elif os.path.isfile(url):
                result = await self.download_from_txt_file(url)
            else:
                result = await self.handle_url(url)

            if isinstance(result, BatchResult):
                results.extend(result.results)
        self.last_result = BatchResult(tuple(results))
        return self.last_result

    async def download_from_txt_file(self, txt_file):
        path = Path(txt_file).resolve()
        if path in self._text_files:
            return BatchResult((DownloadResult(DownloadStatus.FAILED, reason="Recursive text-file inclusion"),))
        self._text_files.add(path)
        try:
            with path.open(encoding="utf-8") as source:
                urls = []
                for line in source:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    local = path.parent / line
                    urls.append(str(local) if local.is_file() else line)
            return await self.download_list_of_urls(urls)
        except (OSError, UnicodeError) as error:
            return BatchResult((DownloadResult(DownloadStatus.FAILED, errors=(type(error).__name__,)),))
        finally:
            self._text_files.remove(path)

    async def lucky_mode(self, query, download=True):
        if len(query) < 3:
            logger.info(f"{RED}Your search query is too short or invalid")
            if download:
                self.last_result = BatchResult(
                    (DownloadResult(DownloadStatus.FAILED, reason="Search query too short"),)
                )
            return

        logger.info(
            f'{YELLOW}Searching {self.lucky_type}s for "{query}".\n'
            f"{YELLOW}qobuz-dl2 will attempt to download the first {self.lucky_limit} results."
        )
        results = await self.search_by_type(query, self.lucky_type, self.lucky_limit, True)

        if download and results:
            self.last_result = await self.download_list_of_urls(results)

        return results

    async def search_by_type(self, query, item_type, limit=10, lucky=False):
        if len(query) < 3:
            logger.info(f"{RED}Your search query is too short or invalid")
            return []

        possibles = {
            "album": {
                "func": self.client.search_albums,
                "key": "albums",
                "format": "{artist[name]} - {title}",
                "requires_extra": True,
            },
            "artist": {
                "func": self.client.search_artists,
                "key": "artists",
                "format": "{name} - ({albums_count} releases)",
                "requires_extra": False,
            },
            "track": {
                "func": self.client.search_tracks,
                "key": "tracks",
                "format": "{performer[name]} - {title}",
                "requires_extra": True,
            },
            "playlist": {
                "func": self.client.search_playlists,
                "key": "playlists",
                "format": "{name} - ({tracks_count} releases)",
                "requires_extra": False,
            },
        }

        try:
            mode_dict = possibles[item_type]
            results = await mode_dict["func"](query, limit)
            iterable = results[mode_dict["key"]]["items"]
            item_list = []
            for i in iterable:
                fmt = PartialFormatter()
                text = fmt.format(mode_dict["format"], **i)
                if mode_dict["requires_extra"]:
                    text = "{} - {} [{}]".format(
                        text,
                        format_duration(i["duration"]),
                        "HI-RES" if i.get("hires_streamable") else "LOSSLESS",
                    )
                url = f"{WEB_URL}{item_type}/{i.get('id', '')}"
                item_list.append({"text": text, "url": url} if not lucky else url)
            return item_list
        except (KeyError, IndexError):
            logger.info(f"{RED}Invalid type: {item_type}")
            return []

    async def interactive(self, download=True):
        try:
            import importlib

            pick_mod = importlib.import_module("pick")
            pick = getattr(pick_mod, "pick")  # type: ignore[attr-defined]
        except (ImportError, ModuleNotFoundError):
            if os.name == "nt":
                sys.exit('Please install curses with "uv pip install windows-curses" to continue')
            raise

        qualities = [
            {"q_string": "320", "q": 5},
            {"q_string": "Lossless", "q": 6},
            {"q_string": "Hi-res =< 96kHz", "q": 7},
            {"q_string": "Hi-Res > 96 kHz", "q": 27},
        ]

        def get_title_text(option):
            return option.get("text")

        def get_quality_text(option):
            return option.get("q_string")

        try:
            item_types = ["Albums", "Tracks", "Artists", "Playlists"]
            selected_type, _ = pick(item_types, "I'll search for:\n[press Intro]")
            selected_type = selected_type[:-1].lower()
            logger.info(f"{YELLOW}Ok, we'll search for {selected_type}s{RESET}")
            final_url_list = []
            while True:
                query = input(f"{CYAN}Enter your search: [Ctrl + c to quit]\n-{RESET} ")
                if not query:
                    continue
                logger.info(f"{YELLOW}Searching...{RESET}")
                options = await self.search_by_type(query, selected_type, self.interactive_limit)
                if not options:
                    logger.info(f"{OFF}Nothing found{RESET}")
                    continue
                title = (
                    f'*** RESULTS FOR "{query.title()}" ***\n\n'
                    "Select [space] the item(s) you want to download (one or more)\n"
                    "Press Ctrl + c to quit\n"
                    "Don't select anything to try another search"
                )
                selected_items = pick(
                    options,
                    title,
                    multiselect=True,
                    min_selection_count=0,
                    options_map_func=get_title_text,
                )
                if selected_items:
                    final_url_list.extend([item[0]["url"] for item in selected_items])
                    y_n, _ = pick(
                        ["Yes", "No"],
                        "Items were added to queue. Keep searching?",
                    )
                    if y_n == "No":
                        break
                else:
                    logger.info(f"{YELLOW}Ok, try again...{RESET}")

            if final_url_list:
                desc = "Select [intro] the quality (fallback enabled)"
                self.quality, _ = pick(qualities, desc, default_index=1, options_map_func=get_quality_text)
                self.quality = self.quality["q"]

                if download:
                    self.last_result = await self.download_list_of_urls(final_url_list)
                return final_url_list
        except KeyboardInterrupt:
            raise

    async def download_lastfm_pl(self, playlist_url):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                from qobuz_dl2.network import get_with_retry

                r = await get_with_retry(client, playlist_url, follow_redirects=True)
                r.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Playlist download failed (%s)", type(e).__name__)
            return BatchResult((DownloadResult(DownloadStatus.FAILED, errors=(type(e).__name__,)),))

        soup = bso(r.content, "html.parser")
        artists = [artist.text for artist in soup.select(ARTISTS_SELECTOR)]
        titles = [title.text for title in soup.select(TITLE_SELECTOR)]

        if not artists or len(artists) != len(titles):
            logger.info(f"{OFF}Nothing found in playlist")
            return BatchResult((DownloadResult(DownloadStatus.SKIPPED, reason="Empty Last.fm playlist"),))

        track_list = [f"{artist} {title}" for artist, title in zip(artists, titles)]
        h1 = soup.select_one("h1")
        pl_title = sanitize_filename(h1.get_text(strip=True) if h1 else "Last.fm Playlist")
        pl_directory = os.path.join(self.directory, pl_title)
        logger.info(f"{YELLOW}Downloading playlist: {pl_title} ({len(track_list)} tracks)")

        async def download_query(query):
            results = await self.search_by_type(query, "track", 1, lucky=True)
            if not results:
                return DownloadResult(DownloadStatus.SKIPPED, reason="No matching track")
            _, track_id = get_url_info(results[0])
            return await self.download_from_id(track_id, False, pl_directory)

        create_and_return_dir(pl_directory)
        outcomes = await bounded_map(download_query, track_list, self.download_limit)
        completed = [
            r if isinstance(r, DownloadResult) else DownloadResult(DownloadStatus.FAILED, errors=(type(r).__name__,))
            for r in outcomes
        ]

        if not self.no_m3u_for_playlists:
            await run_owned_thread(make_m3u, pl_directory, BatchResult(tuple(completed)).artifacts)
        return BatchResult(tuple(completed))
