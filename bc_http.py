"""
One place for every HTTP call to Business Central, with throttle handling.

BC allows only FIVE concurrent OData/API requests per environment, and
600 requests/minute on production (300 on sandbox). Exceed either and BC
replies 429, or 503 once a request has sat in its queue for 8 minutes.

Before this module existed, the mirror called requests.get() + raise_for_status()
directly, so a 429 became an exception, the event dead-lettered, and that
record silently never synced. Every BC call now goes through here and backs
off instead.

Ref: https://learn.microsoft.com/en-us/dynamics365/business-central/
     dev-itpro/administration/operational-limits-online
"""

import logging
import random
import time

import requests

from bc_auth import get_bc_token

log = logging.getLogger("bc_http")

DEFAULT_TIMEOUT = 30
MAX_ATTEMPTS = 5

# Statuses worth retrying. 429 = throttled, 503 = queue timeout,
# 408 = operation timeout, 5xx = transient BC/service fault.
RETRY_STATUSES = (408, 429, 500, 502, 503, 504)

# Never sleep longer than this on a single backoff, whatever Retry-After says.
MAX_BACKOFF_SECONDS = 60


class BcError(RuntimeError):
    """A BC call failed. Carries status and body so callers can explain why."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def retryable(self) -> bool:
        if self.status is None:
            return True
        return self.status in RETRY_STATUSES


def _retry_after_seconds(resp, attempt: int) -> float:
    """
    Honour Retry-After when BC sends it; otherwise exponential backoff with
    jitter. Jitter matters because several Function instances get throttled at
    the same moment and would otherwise all retry in lockstep.
    """
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass   # could be an HTTP-date; fall through to backoff

    return min(2 ** attempt + random.uniform(0, 1), MAX_BACKOFF_SECONDS)


def bc_request(method: str, url: str, *, json_body=None, etag: str | None = None,
               timeout: int = DEFAULT_TIMEOUT, max_attempts: int = MAX_ATTEMPTS,
               allow_404: bool = False, data: bytes | None = None):
    """
    Call BC with throttle-aware retries.

    allow_404=True returns None instead of raising, for "does this exist" reads
    and for a document that has already been deleted in BC.
    """
    headers = {
        "Authorization": f"Bearer {get_bc_token()}",
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"   # file upload
    if etag:
        headers["If-Match"] = etag      # BC rejects PATCH without this

    last = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, headers=headers,
                                    json=json_body, data=data,
                                    timeout=timeout)
        except requests.exceptions.RequestException as e:
            # connection reset / DNS / read timeout - retry
            last = BcError(f"{method} {url} failed: {e}")
            if attempt == max_attempts:
                raise last
            time.sleep(min(2 ** attempt + random.uniform(0, 1),
                           MAX_BACKOFF_SECONDS))
            continue

        if resp.status_code == 404 and allow_404:
            return None

        if resp.status_code in RETRY_STATUSES:
            wait = _retry_after_seconds(resp, attempt)
            log.warning(
                "BC %s on %s %s - backing off %.1fs (attempt %s/%s)",
                resp.status_code, method, url, wait, attempt, max_attempts,
            )
            last = BcError(f"{method} {url} -> {resp.status_code}",
                           status=resp.status_code, body=resp.text[:1500])
            if attempt == max_attempts:
                raise last
            time.sleep(wait)
            continue

        if resp.status_code >= 400:
            # 400/401/403/409/422 etc - a real rejection, retrying won't help
            raise BcError(f"{method} {url} -> {resp.status_code}",
                          status=resp.status_code, body=resp.text[:1500])

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    raise last if last else BcError(f"{method} {url}: exhausted attempts")


def bc_get(url: str, *, allow_404: bool = False,
           timeout: int = DEFAULT_TIMEOUT):
    return bc_request("GET", url, allow_404=allow_404, timeout=timeout)


def bc_get_all(url: str, *, allow_404: bool = False,
               timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Follow @odata.nextLink and return every page's rows."""
    rows: list[dict] = []
    page = bc_get(url, allow_404=allow_404, timeout=timeout)

    while page:
        rows.extend(page.get("value", []))
        nxt = page.get("@odata.nextLink")
        page = bc_get(nxt, timeout=timeout) if nxt else None

    return rows
