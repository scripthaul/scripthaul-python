# ScriptHaul for Python

Dependency-free, synchronous ScriptHaul API client for Python 3.9+. MIT licensed.

```sh
pip install scripthaul
```

Until the first PyPI release, install straight from GitHub: `pip install git+https://github.com/scripthaul/scripthaul-python`. Issues and pull requests are welcome in this repository; the API itself is documented at [api.scripthaul.com/docs](https://api.scripthaul.com/docs).

```python
from scripthaul import ScriptHaul

client = ScriptHaul(api_key="sh_live_...")
transcript = client.transcript("dQw4w9WgXcQ")
print(transcript.meta.cache, transcript.meta.credits_charged)  # HIT 0
videos = client.videos("https://www.youtube.com/@example")
job = client.jobs.create(input="https://www.youtube.com/@example").wait()
```

## Transcripts

`client.transcript(video_id, **options)` accepts `translate` (default `False`). Set `language="es", translate=True` to receive the requested language when YouTube declines to translate the captions, if it can be delivered. A transcript delivered this way costs 300 credits per started 10,000 characters of transcript (at most 1,500), on top of the delivery credit; nothing extra is charged when it cannot be delivered. Without `translate`, nothing changes.

## Jobs

`client.jobs.create()` accepts `translate` (default `False`) alongside `input`, `language`, `fallback`, and `webhook_url`. Set `translate=True` to ask for the job's language on videos whose captions YouTube declines to translate. A transcript delivered this way costs 300 credits per started 10,000 characters of transcript (at most 1,500), on top of the delivery credit; nothing extra is charged when it cannot be delivered. These translations are charged per video when delivered, never reserved. Opted-in jobs carry `translate: true` and a `translation_credits` total; their video rows carry `translation_credits`. Without `translate`, nothing changes.

## Discovery

Search YouTube or one channel for one credit per delivered page. Identical requests within 60 seconds are free retries. Channel resolution and the latest 15 channel or playlist entries cost zero credits; latest entries come from a feed cached for ten minutes.

```python
page = client.search("climate science", type="video", channel="@TED")
print(page["results"], page.meta.credits_charged)  # video results include cached: True/False
if page["has_more"]:
    next_page = client.search("climate science", type="video", channel="@TED",
                              continuation=page["continuation"])
channel = client.channels.resolve("@TED")
latest = client.channels.latest(channel=channel["channel_id"])
playlist_latest = client.channels.latest(playlist=channel["uploads_playlist_id"])
```

`type` accepts `video` (default), `channel`, or `playlist`. Keep continuation strings unchanged and repeat the same query, type, and channel. Resolve a handle first and pass its `channel_id` to latest uploads so that reading the feed needs no Data API call. `examples/discovery.py` requests two search pages, one credit per delivered page.

## Library and monitors

Library coverage and scoped passage search cost zero credits. Fetch any missing transcripts through a normal job first; each successful cold delivery costs one credit and joins the shared library. Search supports quoted phrases and prefix terms and requires one channel or video scope.

Coverage reports `counts.uncaptioned` for known `no_captions` or `unavailable` videos without a searchable index. Those videos are excluded from `missing` and `counts.cold`; an existing index takes precedence. `complete: true` means the source snapshot is complete and nothing indexable is missing, so it can coexist with a positive uncaptioned count. Report that count without promising captions for every video.

```python
coverage = client.library.coverage(channel="@TED")
passages = client.library.search('"solar energy"', channel="@TED", limit=10, offset=0)
print(passages.meta.library_coverage)  # indexed/known, for example "37/412"
watched = client.monitors.create(channel="@TED", auto_fetch=False)
monitors = client.monitors.list()
monitor = client.monitors.get(watched["monitor"]["id"])
client.monitors.cancel(watched["monitor"]["id"])
```

Coverage also accepts `playlist` instead of `channel`; passage search accepts `video_id` instead of `channel`. Monitors accept either `channel` or `playlist`, plus `language`, `fallback`, and `webhook_url`. Set `auto_fetch=True` to authorize ordinary billable transcript jobs for new uploads; monitoring itself costs zero credits. The executable `examples/library.py` uses `auto_fetch=False` and cancels its monitor before exiting.

Monitor creation resolves handles on the server through the channel-resolution allowance; direct UC IDs need no resolution call. Each `monitors.create()` generates one idempotency key and reuses it for retries. To recover a creation across separate calls, pass `client.monitors.create(channel="@TED", idempotency_key="my-watch-creation")`. An explicit key must be a nonblank header-safe string of at most 128 characters; invalid keys fail locally. Keys are sent only in the `Idempotency-Key` header. A new watch returns status 201 and `idempotent: false`; replaying a key or creating an already active/paused account source returns 200 and `idempotent: true`, with the existing monitor unchanged. Replaying a key still returns that original monitor after cancellation; use a fresh key for a replacement.

## Response metadata

Every successful JSON body is returned as an `ApiResponse` (a `dict`) whose `meta` attribute carries the header contract: `cache` (`HIT`/`MISS`), `credits_charged`, `credits_balance`, `rate_limit`, `request_id`, `library_coverage`, and `status`. It is an attribute, not a key, so it never appears in `json.dumps` or in dict iteration. Jobs expose the same object as `job.meta`, refreshed on every read.

## Errors

API failures are subclasses of `ScriptHaulError` named exactly after the server's `errorClass`. Every exception has `error_class`, `code`, `status`, `retryable`, `retry_after` (seconds, from `Retry-After` or the envelope), `request_id`, `docs_url`, and `details`.

`NotFound` with status 404 and code `feed_not_found` means YouTube returned 404 for a well-formed channel or playlist RSS feed. Latest uploads and monitor creation raise it without automatic retries; check the source ID before trying again.

Failures the server never saw use client-only classes whose `error_class` is `None`, so a local problem is never mistaken for a relay verdict: `ClientNetworkError` (`code="network_error"`, the socket failed), `ClientTimeout` (`poll_timeout` or `job_wait_timeout`, the client's own budget ran out), and `JobPaused` (`job_paused`). All three are `retryable`.

## Retries and polling

POST requests are retried only when they carry a valid `Idempotency-Key`; an unkeyed POST gets one transport attempt even if the connection fails or the response is retryable. Both `jobs.create()` and `monitors.create()` supply stable keys. `job.cancel()` has no key and is not automatically retried.

Retryable requests use `max_retries` (default three) for network failures, 502, 503, and retryable 429 responses. YouTube and channel search (`GET /v1/search`) share a stricter total limit of **at most one retry**, even when `max_retries` is higher, because each failed attempt can spend proxy bytes. `max_retries=0` disables that retry too. The query and continuation stay unchanged. Other GETs, including library search, retain the configured limit. A response whose envelope says `retryable: false` (every daily cap, and the per-key rpm guard) is raised immediately with `retry_after` preserved; so is one whose `Retry-After` exceeds `max_retry_after` (default 30 s). Transcript 202 responses are polled along the API's `poll_url` (same origin only) up to `max_polls` times.

`job.wait()` polls until the job is `completed`, `failed`, or `cancelled`. A `paused` job stops the wait: `InsufficientCredits` (`code="job_paused_insufficient_credits"`, with `pause_reason`, `credits`, and `job` in `details`) when the account must buy credits, otherwise `JobPaused`. Pass `wait_through_pauses=True` (per call or on the client) to keep polling instead. `job.cancel()` releases the unsettled reservation.

## Key handling

`repr(client)`, `str(client)`, and `vars(client)` show only the 12-character display prefix (`client.key_prefix`); the full key is reachable as `client.api_key` and is sent solely as `Authorization: Bearer` to `base_url`. The client refuses cross-origin poll URLs and never follows redirects.
