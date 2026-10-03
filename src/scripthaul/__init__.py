"""Dependency-free ScriptHaul API client."""

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_BASE_URL = "https://api.scripthaul.com"
RETRY_STATUSES = {429, 502, 503}
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
# Polls are bounded separately from retries: a 202 Retry-After is the server's
# pacing hint, never a reason to give up.
MAX_POLL_DELAY = 30.0
# Fallback for a source checkout that was never installed; the test suite pins
# it to pyproject.toml so the two cannot drift.
_FALLBACK_VERSION = "0.2.0"


def _detect_version():
    try:
        return _installed_version("scripthaul")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


__version__ = _detect_version()
SDK_HEADER = "python/%s" % __version__

_UNSET = object()


class ScriptHaulError(Exception):
    """Base API exception; request_id is always present, or None client-side."""

    error_class = "UpstreamError"

    def __init__(self, message, *, error_class=_UNSET, code="unexpected_error",
                 status=None, retryable=False, retry_after=None, request_id=None,
                 docs_url=None, details=None):
        super().__init__(message)
        # Server classes pin their error_class on the class. Client-only
        # classes pin None, so a local socket failure or an undersized timeout
        # can never be mistaken for a relay verdict.
        self.error_class = (self.__class__.error_class if error_class is _UNSET
                            else error_class)
        self.errorClass = self.error_class
        self.code = code
        self.status = status
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.request_id = request_id
        self.docs_url = docs_url
        self.details = details or {}


class IpBlocked(ScriptHaulError): error_class = "IpBlocked"
class PoTokenRequired(ScriptHaulError): error_class = "PoTokenRequired"
class RateLimited(ScriptHaulError): error_class = "RateLimited"
class Unavailable(ScriptHaulError): error_class = "Unavailable"
class NoCaptions(ScriptHaulError): error_class = "NoCaptions"
class UpstreamError(ScriptHaulError): error_class = "UpstreamError"
class RelayUnavailable(ScriptHaulError): error_class = "RelayUnavailable"
class BudgetExhausted(ScriptHaulError): error_class = "BudgetExhausted"
class InvalidInput(ScriptHaulError): error_class = "InvalidInput"
class NotFound(ScriptHaulError): error_class = "NotFound"
class QuotaExceeded(ScriptHaulError): error_class = "QuotaExceeded"
class TurnstileFailed(ScriptHaulError): error_class = "TurnstileFailed"
class Forbidden(ScriptHaulError): error_class = "Forbidden"
class InsufficientCredits(ScriptHaulError): error_class = "InsufficientCredits"
class AccountFrozen(ScriptHaulError): error_class = "AccountFrozen"
class ConfigurationError(ScriptHaulError): error_class = "ConfigurationError"


# Client-only failures: no server verdict exists because the request did not
# complete, or the caller's own budget ran out first.
class ClientNetworkError(ScriptHaulError): error_class = None
class ClientTimeout(ScriptHaulError): error_class = None
class JobPaused(ScriptHaulError): error_class = None


ERROR_CLASSES = {
    cls.error_class: cls for cls in (
        IpBlocked, PoTokenRequired, RateLimited, Unavailable, NoCaptions,
        UpstreamError, RelayUnavailable, BudgetExhausted, InvalidInput, NotFound,
        QuotaExceeded, TurnstileFailed, Forbidden, InsufficientCredits,
        AccountFrozen, ConfigurationError,
    )
}


def _valid_idempotency_key(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 128
            and re.search(r"[\x00-\x08\x0a-\x1f\x7f\u0100-\U0010ffff]", value) is None)


def _monitor_idempotency_key(value):
    if value is None:
        return str(uuid.uuid4())
    if not _valid_idempotency_key(value):
        raise InvalidInput("Idempotency-Key must contain 1 to 128 header-safe characters.",
                           code="invalid_idempotency_key")
    return value


def _has_idempotency_key(headers):
    values = [value for name, value in (headers or {}).items()
              if str(name).lower() == "idempotency-key"]
    # An ambiguous pair of differently-cased headers is not a replay identity.
    return len(values) == 1 and _valid_idempotency_key(values[0])


class ResponseMeta:
    """The header contract of one response: cache verdict, credits, limits, id."""

    __slots__ = ("status", "cache", "credits_charged", "credits_balance",
                 "rate_limit", "request_id", "library_coverage")

    def __init__(self, status, headers):
        self.status = status
        self.cache = headers.get("x-cache") or None
        self.credits_charged = _header_number(headers, "x-credits-charged")
        self.credits_balance = _header_number(headers, "x-credits-balance")
        self.rate_limit = _header_number(headers, "x-ratelimit-limit")
        self.request_id = headers.get("x-request-id") or None
        self.library_coverage = headers.get("x-library-coverage") or None

    def __repr__(self):
        return ("ResponseMeta(status=%r, cache=%r, credits_charged=%r, "
                "credits_balance=%r, rate_limit=%r, request_id=%r)" % (
                    self.status, self.cache, self.credits_charged,
                    self.credits_balance, self.rate_limit, self.request_id))


class ApiResponse(dict):
    """A JSON body that also carries the response headers as ``meta``."""

    __slots__ = ("meta",)

    def __init__(self, payload, meta=None):
        super().__init__(payload)
        self.meta = meta


class _Secret:
    """Holds the key so ``vars(client)``, ``repr`` and pretty-printers show a prefix only."""

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value

    def __repr__(self):
        return "<secret %s...>" % self._value[:12]

    __str__ = __repr__


def _header_number(headers, name):
    value = headers.get(name)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirect)


def _default_transport(method, url, headers, body, timeout):
    request = Request(url, data=body, headers=headers, method=method)
    try:
        # Treat every redirect as an API error. urllib otherwise reuses the
        # Request headers and can disclose the Bearer key to another origin.
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def _retry_after_seconds(headers, payload):
    """Retry-After from the header (seconds or HTTP-date), else the envelope; None without a hint."""
    value = headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    if isinstance(payload, dict):
        try:
            return max(0.0, float(payload["retry_after"]))
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _poll_delay(headers, fallback):
    hinted = _retry_after_seconds(headers, None)
    return min(MAX_POLL_DELAY, fallback if hinted is None else hinted)


def _query(values):
    clean = {}
    for key, value in values.items():
        if value is None:
            continue
        clean[key] = str(value).lower() if isinstance(value, bool) else value
    return urlencode(clean)


def _paused_error(job):
    reason = job.data.get("pause_reason")
    details = {"pause_reason": reason, "credits": job.data.get("credits"), "job": job.data}
    request_id = job.meta.request_id if job.meta else None
    if reason == "insufficient_credits":
        error = InsufficientCredits(
            "Job %s is paused until the account buys credits "
            "(pause_reason insufficient_credits)." % job.id,
            code="job_paused_insufficient_credits", retryable=False,
            details=details, request_id=request_id,
        )
    else:
        error = JobPaused(
            "Job %s is paused (%s); pass wait_through_pauses=True to keep polling."
            % (job.id, reason or "reason not given"),
            code="job_paused", retryable=True, details=details, request_id=request_id,
        )
    error.job = job
    return error


class Job:
    """Mutable view of one bulk job."""

    def __init__(self, client, data):
        if not isinstance(data, dict) or not data.get("id"):
            raise InvalidInput("The API returned a job without an id.",
                               code="invalid_job_response")
        self._client = client
        self.data = data

    @property
    def id(self):
        return self.data["id"]

    @property
    def status(self):
        return self.data.get("status")

    @property
    def meta(self):
        return getattr(self.data, "meta", None)

    def refresh(self):
        payload = self._client._request("GET", "/v1/jobs/%s" % self.id)
        self.data = _unwrap_job(payload)
        return self

    def cancel(self):
        """Cancel the job, releasing its unsettled reservation; delivered rows keep their charge."""
        payload = self._client._request("POST", "/v1/jobs/%s/cancel" % self.id)
        job = payload.get("job") if isinstance(payload, dict) else None
        if isinstance(job, dict) and job.get("id"):
            self.data = _unwrap_job(payload)
            return self
        return self.refresh()

    def wait(self, *, poll_interval=None, timeout=None, wait_through_pauses=None):
        interval = (self._client.job_poll_interval if poll_interval is None
                    else poll_interval)
        limit = self._client.job_timeout if timeout is None else timeout
        through_pauses = (self._client.wait_through_pauses if wait_through_pauses is None
                          else wait_through_pauses)
        deadline = self._client._monotonic() + limit
        while True:
            if self.status in TERMINAL_JOB_STATUSES:
                return self
            # A paused job waits for a purchase or the cron, not for this loop:
            # polling it every two seconds for ten minutes is ~300 metered
            # reads that cannot change the outcome.
            if self.status == "paused" and not through_pauses:
                raise _paused_error(self)
            if self._client._monotonic() >= deadline:
                raise ClientTimeout(
                    "Timed out waiting for job %s." % self.id,
                    code="job_wait_timeout", retryable=True,
                    request_id=self.meta.request_id if self.meta else None,
                )
            if interval > 0:
                self._client._sleep(interval)
            self.refresh()

    def __repr__(self):
        return "Job(id=%r, status=%r)" % (self.id, self.status)


def _unwrap_job(payload):
    job = payload.get("job", payload)
    if isinstance(job, dict) and not isinstance(job, ApiResponse):
        job = ApiResponse(job, getattr(payload, "meta", None))
    return job


class Jobs:
    def __init__(self, client):
        self._client = client

    def create(self, input=None, *, translate=None, idempotency_key=None, **options):
        if not input:
            raise InvalidInput("jobs.create() requires an input.",
                               code="missing_job_input")
        if isinstance(input, dict):
            payload = {**input, **options}
        else:
            payload = {"input": input, **options}
        if translate is not None:
            payload["translate"] = translate
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        result = self._client._request("POST", "/v1/jobs", body=payload,
                                      headers=headers)
        return Job(self._client, _unwrap_job(result))


class Channels:
    def __init__(self, client):
        self._client = client

    def resolve(self, input):
        if not input:
            raise InvalidInput("channels.resolve() requires a channel input.",
                               code="missing_channel_input")
        return self._client._request("GET", "/v1/channels/resolve?" + _query({"input": input}))

    def latest(self, *, channel=None, playlist=None):
        if bool(channel) == bool(playlist):
            raise InvalidInput("channels.latest() requires exactly one channel or playlist.",
                               code="invalid_latest_scope")
        return self._client._request("GET", "/v1/channels/latest?" + _query({
            "channel": channel, "playlist": playlist,
        }))


class Library:
    def __init__(self, client):
        self._client = client

    def coverage(self, *, channel=None, playlist=None):
        if bool(channel) == bool(playlist):
            raise InvalidInput("library.coverage() requires exactly one channel or playlist.",
                               code="invalid_library_scope")
        return self._client._request("GET", "/v1/library?" + _query({
            "channel": channel, "playlist": playlist,
        }))

    def search(self, query, *, channel=None, video_id=None, limit=None, offset=None):
        if not query or bool(channel) == bool(video_id):
            raise InvalidInput("library.search() requires a query and exactly one channel or video_id.",
                               code="invalid_library_search")
        return self._client._request("GET", "/v1/library/search?" + _query({
            "q": query, "channel": channel, "video_id": video_id,
            "limit": limit, "offset": offset,
        }))


class Monitors:
    def __init__(self, client):
        self._client = client

    def create(self, *, channel=None, playlist=None, auto_fetch=False,
               language=None, fallback=None, webhook_url=None, idempotency_key=None):
        if bool(channel) == bool(playlist):
            raise InvalidInput("monitors.create() requires exactly one channel or playlist.",
                               code="invalid_monitor_scope")
        body = {key: value for key, value in {
            "channel": channel, "playlist": playlist, "auto_fetch": auto_fetch,
            "language": language, "fallback": fallback, "webhook_url": webhook_url,
        }.items() if value is not None}
        headers = {"Idempotency-Key": _monitor_idempotency_key(idempotency_key)}
        return self._client._request("POST", "/v1/monitors", body=body, headers=headers)

    def list(self):
        return self._client._request("GET", "/v1/monitors")

    def get(self, id):
        if not id:
            raise InvalidInput("monitors.get() requires a monitor id.", code="missing_monitor_id")
        return self._client._request("GET", "/v1/monitors/" + quote(str(id), safe=""))

    def cancel(self, id):
        if not id:
            raise InvalidInput("monitors.cancel() requires a monitor id.", code="missing_monitor_id")
        return self._client._request("DELETE", "/v1/monitors/" + quote(str(id), safe=""))


class ScriptHaul:
    """Synchronous client for discovery, transcripts, listings, and bulk jobs."""

    def __init__(self, api_key=None, *, base_url=None, timeout=30,
                 max_retries=3, retry_delay=0.5, max_retry_after=30.0,
                 max_polls=120, poll_interval=1.0, job_poll_interval=2.0,
                 job_timeout=600, wait_through_pauses=False,
                 transport=None, sleep=time.sleep, monotonic=time.monotonic):
        key = api_key or os.environ.get("SCRIPTHAUL_API_KEY")
        if not key:
            raise InvalidInput("Set api_key or SCRIPTHAUL_API_KEY.",
                               code="missing_api_key")
        # Wrapped so nothing that walks the instance (vars, pprint, a
        # structured logger) can print the key. Keys are never logged.
        self._api_key = _Secret(str(key))
        self.key_prefix = str(key)[:12]
        selected_url = base_url or os.environ.get("SCRIPTHAUL_BASE_URL")
        self.base_url = (selected_url or DEFAULT_BASE_URL).rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InvalidInput("base_url must be an absolute HTTP URL.",
                               code="invalid_base_url")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        # Longest Retry-After the client sleeps through. Daily caps advertise
        # a day; nobody wants a call that blocks until UTC midnight.
        self.max_retry_after = max_retry_after
        self.max_polls = max_polls
        self.poll_interval = poll_interval
        self.job_poll_interval = job_poll_interval
        self.job_timeout = job_timeout
        self.wait_through_pauses = bool(wait_through_pauses)
        self._transport = transport or _default_transport
        self._sleep = sleep
        self._monotonic = monotonic
        self.jobs = Jobs(self)
        self.channels = Channels(self)
        self.library = Library(self)
        self.monitors = Monitors(self)

    @property
    def api_key(self):
        return self._api_key.get()

    def __repr__(self):
        return "ScriptHaul(base_url=%r, api_key='%s...')" % (self.base_url, self.key_prefix)

    __str__ = __repr__

    def _url(self, path):
        url = urljoin(self.base_url + "/", path)
        if urlparse(url).netloc != urlparse(self.base_url).netloc or \
                urlparse(url).scheme != urlparse(self.base_url).scheme:
            raise InvalidInput("The API returned a cross-origin polling URL.",
                               code="unsafe_poll_url")
        return url

    @staticmethod
    def _payload(raw):
        if not raw:
            return None
        text = raw.decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _error(status, headers, payload):
        body = payload if isinstance(payload, dict) else {}
        error_class = body.get("errorClass", "UpstreamError")
        error_type = ERROR_CLASSES.get(error_class, ScriptHaulError)
        hinted = _retry_after_seconds(headers, body)
        return error_type(
            body.get("message", "ScriptHaul returned HTTP %s." % status),
            error_class=error_class, code=body.get("code", "unexpected_error"),
            status=status, retryable=body.get("retryable", False),
            retry_after=None if hinted is None else int(-(-hinted // 1)),
            request_id=body.get("request_id") or headers.get("x-request-id"),
            docs_url=body.get("docs_url"), details=body,
        )

    def _request(self, method, path, *, body=None, headers=None, poll_202=False):
        url = self._url(path)
        request_method = str(method).upper()
        request_body = body
        polls = 0
        while True:
            # Without a replay key, an interrupted POST may already have
            # committed. A later 202-to-GET poll keeps the ordinary retry cap.
            # Discovery search failures still spend proxy bytes, so their
            # automatic retry is capped at one even with a higher global cap.
            retry_limit = (0 if request_method == "POST" and not _has_idempotency_key(headers)
                           else min(self.max_retries, 1) if request_method == "GET"
                           and urlparse(url).path.endswith("/v1/search")
                           else self.max_retries)
            response = None
            payload = None
            payload_read = False
            for attempt in range(retry_limit + 1):
                payload = None
                payload_read = False
                request_headers = {
                    "Authorization": "Bearer %s" % self._api_key.get(),
                    "Accept": "application/json",
                    "X-ScriptHaul-SDK": SDK_HEADER,
                    **(headers or {}),
                }
                encoded = None
                if request_body is not None:
                    request_headers["Content-Type"] = "application/json"
                    encoded = json.dumps(request_body, separators=(",", ":")).encode()
                try:
                    status, response_headers, raw = self._transport(
                        request_method, url, request_headers, encoded, self.timeout,
                    )
                    normalized = {str(key).lower(): value
                                  for key, value in response_headers.items()}
                    response = (status, normalized, raw)
                except (OSError, URLError) as cause:
                    if attempt < retry_limit:
                        self._sleep(self.retry_delay * (2 ** attempt))
                        continue
                    raise ClientNetworkError(
                        "The ScriptHaul API could not be reached.",
                        code="network_error", retryable=True,
                        details={"cause": str(cause)},
                    ) from cause
                if status not in RETRY_STATUSES or attempt >= retry_limit:
                    break
                payload = self._payload(raw)
                payload_read = True
                # The envelope's verdict wins: a daily cap says retryable false
                # with a day-long Retry-After, and three more requests cannot
                # change that.
                if isinstance(payload, dict) and payload.get("retryable") is False:
                    break
                delay = _retry_after_seconds(normalized, payload)
                if delay is not None and delay > self.max_retry_after:
                    break
                self._sleep(self.retry_delay * (2 ** attempt) if delay is None else delay)

            status, response_headers, raw = response
            if not payload_read:
                payload = self._payload(raw)
            if status == 202 and poll_202:
                polls += 1
                if polls > self.max_polls:
                    request_id = payload.get("request_id") if isinstance(payload, dict) else None
                    raise ClientTimeout(
                        "Timed out waiting for the transcript.", code="poll_timeout",
                        retryable=True, request_id=request_id or response_headers.get("x-request-id"),
                    )
                if not isinstance(payload, dict) or not payload.get("poll_url"):
                    raise self._error(status, response_headers, payload)
                self._sleep(_poll_delay(response_headers, self.poll_interval))
                url = self._url(payload["poll_url"])
                request_method, request_body = "GET", None
                continue
            if not 200 <= status < 300:
                raise self._error(status, response_headers, payload)
            if isinstance(payload, dict):
                return ApiResponse(payload, ResponseMeta(status, response_headers))
            return payload

    def transcript(self, video_id, **options):
        if not video_id:
            raise InvalidInput("transcript() requires a video id.",
                               code="missing_video_id")
        allowed = {key: options[key] for key in
                   ("format", "language", "fallback", "raw", "fresh", "translate") if key in options}
        query = _query({"video_id": video_id, **allowed})
        return self._request("GET", "/v1/transcript?" + query, poll_202=True)

    def videos(self, input, **options):
        if not input:
            raise InvalidInput(
                "videos() requires a channel, playlist, or list input.",
                code="missing_video_input",
            )
        allowed = {key: options[key] for key in ("contents", "offset") if key in options}
        query = _query({"url": input, **allowed})
        return self._request("GET", "/v1/videos?" + query)

    def search(self, query, *, type=None, channel=None, continuation=None):
        if not query:
            raise InvalidInput("search() requires a query.", code="missing_search_query")
        return self._request("GET", "/v1/search?" + _query({
            "q": query, "type": type, "channel": channel, "continuation": continuation,
        }))


__all__ = [
    "ScriptHaul", "Job", "ApiResponse", "ResponseMeta", "ScriptHaulError",
    "ClientNetworkError", "ClientTimeout", "JobPaused", "SDK_HEADER",
    "__version__", *ERROR_CLASSES.keys(),
]
