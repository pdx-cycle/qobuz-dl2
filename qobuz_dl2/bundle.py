import base64
import logging
import re
from collections import OrderedDict

import httpx

from qobuz_dl2.network import get_sync_with_retry

# Modified code based on DashLt's spoofbuz

logger = logging.getLogger(__name__)

_SEED_TIMEZONE_REGEX = re.compile(r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)')
_INFO_EXTRAS_REGEX = r'name:"\w+/(?P<timezone>{timezones})",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
_APP_ID_REGEX = re.compile(r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"\w{32}"')

_BUNDLE_URL_REGEX = re.compile(r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>')

_BASE_URL = "https://play.qobuz.com"


class Bundle:
    def __init__(self):
        # The client is only needed to fetch the bundle once, so use it as a
        # context manager to avoid leaking the connection pool.
        with httpx.Client(follow_redirects=True, timeout=10.0) as client:
            logger.debug("Getting login page")
            response = get_sync_with_retry(client, f"{_BASE_URL}/login")
            response.raise_for_status()

            bundle_url_match = _BUNDLE_URL_REGEX.search(response.text)
            if not bundle_url_match:
                raise NotImplementedError("Bundle URL not found")

            bundle_url = bundle_url_match.group(1)

            logger.debug("Getting bundle")
            response = get_sync_with_retry(client, _BASE_URL + bundle_url)
            response.raise_for_status()

            self._bundle = response.text

    def get_app_id(self):
        match = _APP_ID_REGEX.search(self._bundle)
        if not match:
            raise NotImplementedError("Failed to match APP ID")

        return match.group("app_id")

    def get_secrets(self):
        logger.debug("Getting secrets")
        seed_matches = _SEED_TIMEZONE_REGEX.finditer(self._bundle)
        secrets = OrderedDict()

        for match in seed_matches:
            seed, timezone = match.group("seed", "timezone")
            secrets[timezone] = [seed]

        keypairs = list(secrets.items())
        if len(keypairs) < 2:
            raise NotImplementedError(
                "Failed to extract app secrets from the Qobuz bundle "
                "(the bundle format may have changed). Try 'qobuz-dl2 -r'."
            )
        secrets.move_to_end(keypairs[1][0], last=False)
        info_extras_regex = _INFO_EXTRAS_REGEX.format(
            timezones="|".join([timezone.capitalize() for timezone in secrets])
        )
        info_extras_matches = re.finditer(info_extras_regex, self._bundle)
        for match in info_extras_matches:
            timezone, info, extras = match.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]
        for secret_pair in secrets:
            parts = secrets[secret_pair]
            try:
                if len(parts) != 3 or not all(parts):
                    raise ValueError("Incomplete secret components")
                encoded = "".join(parts)
                if len(encoded) <= 44:
                    raise ValueError("Missing encoded secret")
                decoded = base64.b64decode(encoded[:-44], validate=True).decode("utf-8").strip()
                if not decoded:
                    raise ValueError("Empty secret")
                secrets[secret_pair] = decoded
            except (ValueError, UnicodeError) as error:
                raise NotImplementedError(
                    "Incomplete app secrets in Qobuz bundle; try resetting the app configuration"
                ) from error
        return secrets
