"""One bounded retry policy for safe GET requests and streaming attempts."""

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


def is_retryable_get_error(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRY_STATUSES
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, ConnectionError))


def retry_delay(error, attempt: int, now=None) -> float:
    if isinstance(error, httpx.HTTPStatusError):
        value = error.response.headers.get("retry-after")
        if value:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    seconds = (date - (now or datetime.now(timezone.utc))).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = None
            if seconds is not None and seconds == seconds:
                return min(60.0, max(0.0, seconds))
    return min(10.0, 2 ** (attempt - 1) + random.uniform(0, 1))


def wait_get_retry(state):
    return retry_delay(state.outcome.exception(), state.attempt_number)


get_retry = retry(
    stop=stop_after_attempt(3), wait=wait_get_retry, retry=retry_if_exception(is_retryable_get_error), reraise=True
)


@get_retry
async def get_with_retry(client, url, **kwargs):
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response


@get_retry
def get_sync_with_retry(client, url, **kwargs):
    response = client.get(url, **kwargs)
    response.raise_for_status()
    return response
