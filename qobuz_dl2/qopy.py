# Wrapper for Qo-DL Reborn. This is a slightly modified version
# of qopy, originally written by Sorrow446. All credits to the
# original author.

import asyncio
import hashlib
import logging
import time
from typing import Callable, Optional

import httpx

from qobuz_dl2.color import GREEN, YELLOW
from qobuz_dl2.exceptions import (
    AuthenticationError,
    IneligibleError,
    InvalidAppSecretError,
    InvalidQuality,
    ResponseError,
    StreamingEndpointRetiredError,
)
from qobuz_dl2.network import get_with_retry

RESET = "Reset your credentials with 'qobuz-dl2 -r'"

# Qobuz removed the direct email/password login API (migrated to an OAuth flow
# protected by reCAPTCHA). Automated login is no longer possible, so we
# authenticate with a `user_auth_token` copied from an active browser session.
TOKEN_HELP = (
    "Your Qobuz token is missing, expired or invalid.\n"
    "Grab a fresh one from your browser:\n"
    "  1. Log in at https://play.qobuz.com\n"
    "  2. Open DevTools > Network and filter by 'login'\n"
    "  3. Click the 'user/login' request, open the Response tab\n"
    "  4. Copy the value of 'user_auth_token'\n"
    "Then run 'qobuz-dl2 -r' and paste it when asked for the token."
)

logger = logging.getLogger(__name__)


class Client:
    def __init__(
        self,
        token: str,
        app_id,
        secrets,
        on_token_refresh: Optional[Callable[[str], None]] = None,
        request_limit: int = 4,
    ):
        logger.info(f"{YELLOW}Logging...")
        if int(request_limit) < 1:
            raise ValueError("Request limit must be positive")
        self.request_limit = int(request_limit)
        self.request_budget = None
        self.secrets = secrets
        self.id = str(app_id)
        self.token = (token or "").strip()
        self.on_token_refresh = on_token_refresh
        self.base = "https://www.qobuz.com/api.json/0.2/"
        self.sec = None
        self.uat = self.token
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0",
                "X-App-Id": self.id,
                "X-User-Auth-Token": self.token,
            },
            timeout=httpx.Timeout(30.0),
        )

    async def initialize(self):
        try:
            await self.auth()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        await self.client.aclose()

    async def auth(self):
        """Authenticate using a stored user_auth_token.

        The token is refreshed through the partner endpoint and, if Qobuz
        returns a new token, persisted back to the config via the
        ``on_token_refresh`` callback so the user does not have to re-copy it
        on every run.
        """
        if not self.token:
            raise AuthenticationError(TOKEN_HELP)

        try:
            r = await self.client.post(self.base + "user/login", data={"extra": "partner"})
        except httpx.HTTPError as e:
            raise AuthenticationError(f"Could not reach Qobuz ({type(e).__name__})") from e

        if r.status_code in (400, 401):
            raise AuthenticationError(TOKEN_HELP)
        r.raise_for_status()
        try:
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
                raise ValueError("Missing user identity")
            user = data["user"]
            identity = user.get("id")
            if isinstance(identity, bool) or not isinstance(identity, (str, int)) or not identity:
                raise ValueError("Missing user identity")
            new_token = data.get("user_auth_token", self.token)
            if not isinstance(new_token, str) or not new_token.strip():
                raise ValueError("Invalid refreshed token")
            credential = user.get("credential", {})
            if not isinstance(credential, dict):
                raise ValueError("Invalid credentials response")
            params = credential.get("parameters")
            if params is not None and not isinstance(params, dict):
                raise ValueError("Invalid membership response")
        except (ValueError, TypeError) as error:
            raise AuthenticationError("Invalid authentication response from Qobuz") from error
        if params is not None and not params:
            raise IneligibleError("Free accounts are not eligible to download tracks.")
        new_token = new_token.strip()
        if new_token != self.token and self.on_token_refresh is not None:
            try:
                self.on_token_refresh(new_token)
                logger.info("Token refreshed and saved")
            except Exception as error:
                logger.warning("Could not save refreshed token (%s)", type(error).__name__)
        self.token = self.uat = new_token
        self.client.headers["X-User-Auth-Token"] = new_token
        self.label = (params or {}).get("short_label", "unknown")
        logger.info(f"{GREEN}Logged: OK\n{GREEN}Membership: {self.label}")

    async def api_call(self, epoint, **kwargs):
        if epoint == "track/get":
            params = {"track_id": kwargs["id"]}
        elif epoint == "album/get":
            params = {"album_id": kwargs["id"]}
        elif epoint == "playlist/get":
            params = {
                "extra": "tracks",
                "playlist_id": kwargs["id"],
                "limit": 500,
                "offset": kwargs["offset"],
            }
        elif epoint == "artist/get":
            params = {
                "artist_id": kwargs["id"],
                "limit": 500,
                "offset": kwargs["offset"],
                "extra": "albums",
            }
        elif epoint == "label/get":
            params = {
                "label_id": kwargs["id"],
                "limit": 500,
                "offset": kwargs["offset"],
                "extra": "albums",
            }
        elif epoint == "track/getFileUrl":
            unix = time.time()
            track_id = kwargs["id"]
            fmt_id = kwargs["fmt_id"]
            if int(fmt_id) not in (5, 6, 7, 27):
                raise InvalidQuality("Invalid quality id: choose between 5, 6, 7 or 27")
            r_sig = f"trackgetFileUrlformat_id{fmt_id}intentstreamtrack_id{track_id}{unix}{kwargs.get('sec', self.sec)}"
            r_sig_hashed = hashlib.md5(r_sig.encode("utf-8")).hexdigest()
            params = {
                "request_ts": unix,
                "request_sig": r_sig_hashed,
                "track_id": track_id,
                "format_id": fmt_id,
                "intent": "stream",
            }
        else:
            params = kwargs

        try:
            if self.request_budget is None:
                self.request_budget = asyncio.Semaphore(self.request_limit)
            async with self.request_budget:
                r = await get_with_retry(self.client, self.base + epoint, params=params)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 401:
                raise AuthenticationError(TOKEN_HELP) from error
            if epoint == "track/getFileUrl" and error.response.status_code == 410:
                raise StreamingEndpointRetiredError(
                    "Qobuz authentication succeeded, but Qobuz retired the legacy "
                    "track/getFileUrl endpoint required by this qobuz-dl2 release. "
                    "Resetting the token or app secrets will not restore downloads; "
                    "a version supporting Qobuz's current session-based segmented streaming is required."
                ) from error
            if epoint == "track/getFileUrl" and error.response.status_code == 400:
                raise InvalidAppSecretError("App secret rejected. " + RESET) from error
            raise
        try:
            data = r.json()
        except ValueError as error:
            raise ResponseError("Qobuz returned invalid JSON") from error
        if not isinstance(data, dict):
            raise ResponseError("Qobuz returned an unexpected response type")
        return data

    async def multi_meta(self, epoint, key, id, type):
        offset = 0
        expected_total = None
        resource = "tracks" if epoint == "playlist/get" else "albums"
        while True:
            j = await self.api_call(epoint, id=id, offset=offset, type=type)
            block = j.get(resource)
            if not isinstance(block, dict):
                raise ResponseError("Missing or invalid collection block")
            items = block.get("items")
            if not isinstance(items, list):
                raise ResponseError("Invalid collection items")
            totals = [container[field] for container, field in ((j, key), (block, "total")) if field in container]
            for total in totals:
                if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                    raise ResponseError("Invalid collection total")
                if expected_total is not None and total != expected_total:
                    raise ResponseError("Collection total changed during enumeration")
                expected_total = total
            if "offset" in block and block["offset"] != offset:
                raise ResponseError("Collection offset did not advance")
            if expected_total is not None:
                if offset + len(items) > expected_total or (not items and offset < expected_total):
                    raise ResponseError("Collection ended before its advertised members were returned")
            yield j
            if not items:
                return
            offset += len(items)
            if expected_total is not None:
                if offset >= expected_total:
                    return
            elif len(items) < 500:
                return

    async def get_album_meta(self, id):
        return await self.api_call("album/get", id=id)

    async def get_track_meta(self, id):
        return await self.api_call("track/get", id=id)

    async def get_track_url(self, id, fmt_id):
        if self.sec is None:
            await self.cfg_setup()
        return await self.api_call("track/getFileUrl", id=id, fmt_id=fmt_id)

    def get_artist_meta(self, id):
        return self.multi_meta("artist/get", "albums_count", id, None)

    def get_plist_meta(self, id):
        return self.multi_meta("playlist/get", "tracks_count", id, None)

    def get_label_meta(self, id):
        return self.multi_meta("label/get", "albums_count", id, None)

    async def search_albums(self, query, limit):
        return await self.api_call("album/search", query=query, limit=limit)

    async def search_artists(self, query, limit):
        return await self.api_call("artist/search", query=query, limit=limit)

    async def search_playlists(self, query, limit):
        return await self.api_call("playlist/search", query=query, limit=limit)

    async def search_tracks(self, query, limit):
        return await self.api_call("track/search", query=query, limit=limit)

    async def test_secret(self, sec):
        try:
            data = await self.api_call("track/getFileUrl", id=5966783, fmt_id=5, sec=sec)
            return isinstance(data.get("url"), str) and bool(data["url"])
        except InvalidAppSecretError:
            return False

    async def cfg_setup(self):
        for secret in self.secrets:
            if not secret:
                continue
            if await self.test_secret(secret):
                self.sec = secret
                return
        raise InvalidAppSecretError(
            "Authentication succeeded, but Qobuz rejected every application secret "
            "for the legacy streaming endpoint. Your login token remains valid; "
            "re-running login will not repair stream setup."
        )
