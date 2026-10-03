import json
import re
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

import scripthaul
from scripthaul import (
    SDK_HEADER, ApiResponse, ClientNetworkError, ClientTimeout, InsufficientCredits,
    InvalidInput, JobPaused, QuotaExceeded, RelayUnavailable, ScriptHaul,
    ScriptHaulError,
)


def response(status, body, headers=None):
    return status, headers or {"content-type": "application/json"}, json.dumps(body).encode()


def job_body(status, request_id="req-job", **extra):
    job = {
        "id": "0123456789abcdef0123456789abcdef", "status": status,
        "counts": {"total": 3, "completed": 1},
        "credits": {"reserved": 2, "charged": 1, "released": 0, "outstanding": 1},
    }
    job.update(extra)
    return response(200, {"ok": True, "job": job}, {
        "content-type": "application/json", "x-request-id": request_id,
    })


class ClientTests(unittest.TestCase):
    def test_transcript_translate_encodes_explicit_booleans(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, urlparse(url), body))
            return response(200, {"ok": True, "text": "Fixture"})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport)
        for value, encoded in [(True, "true"), (False, "false")]:
            with self.subTest(translate=value):
                client.transcript("dQw4w9WgXcQ", language="fr", translate=value)
                method, url, body = calls[-1]
                self.assertEqual(method, "GET")
                self.assertEqual(url.path, "/v1/transcript")
                self.assertEqual(parse_qs(url.query), {
                    "video_id": ["dQw4w9WgXcQ"], "language": ["fr"],
                    "translate": [encoded],
                })
                self.assertIsNone(body)

    def test_job_create_preserves_translate_in_body(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, urlparse(url), json.loads(body)))
            return response(202, {"ok": True, "job": {"id": "job-1", "status": "pending"}})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport)
        for value in [True, False]:
            with self.subTest(translate=value):
                client.jobs.create(input="https://youtube.com/@fixture", language="fr", translate=value)
                method, url, body = calls[-1]
                self.assertEqual(method, "POST")
                self.assertEqual(url.path, "/v1/jobs")
                self.assertEqual(url.query, "")
                self.assertEqual(body, {
                    "input": "https://youtube.com/@fixture", "language": "fr", "translate": value,
                })
                self.assertIs(body["translate"], value)
        client.jobs.create({"input": "https://youtube.com/@fixture", "translate": True})
        self.assertIs(calls[-1][2]["translate"], True)
        client.jobs.create({"input": "https://youtube.com/@fixture", "translate": True}, translate=False)
        self.assertIs(calls[-1][2]["translate"], False)

    def test_translate_is_omitted_when_unset(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            parsed = urlparse(url)
            calls.append((parse_qs(parsed.query), json.loads(body) if body else None))
            if method == "POST":
                return response(202, {"ok": True, "job": {"id": "job-1", "status": "pending"}})
            return response(200, {"ok": True, "text": "Fixture"})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport)
        for options in [{}, {"translate": None}]:
            with self.subTest(options=options):
                client.transcript("dQw4w9WgXcQ", **options)
                self.assertEqual(calls[-1], ({"video_id": ["dQw4w9WgXcQ"]}, None))
                client.jobs.create(input="https://youtube.com/@fixture", **options)
                self.assertEqual(calls[-1], ({}, {"input": "https://youtube.com/@fixture"}))

    def test_discovery_encodes_queries_and_preserves_credit_headers(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            parsed = urlparse(url)
            calls.append((method, parsed.path, parse_qs(parsed.query)))
            return response(200, {"ok": True}, {
                "content-type": "application/json",
                "x-credits-charged": "1" if parsed.path == "/v1/search" else "0",
            })

        client = ScriptHaul("sh_test_unit_fixture", transport=transport)
        page = client.search("science & art", type="video", channel="@TED", continuation="abc_-/+=")
        self.assertEqual(page.meta.credits_charged, 1)
        resolved = client.channels.resolve("https://www.youtube.com/@TED")
        self.assertEqual(resolved.meta.credits_charged, 0)
        client.channels.latest(channel="UC0123456789abcdefghijkl")
        client.channels.latest(playlist="UU0123456789abcdefghijkl")
        self.assertEqual(calls, [
            ("GET", "/v1/search", {"q": ["science & art"], "type": ["video"], "channel": ["@TED"], "continuation": ["abc_-/+="]}),
            ("GET", "/v1/channels/resolve", {"input": ["https://www.youtube.com/@TED"]}),
            ("GET", "/v1/channels/latest", {"channel": ["UC0123456789abcdefghijkl"]}),
            ("GET", "/v1/channels/latest", {"playlist": ["UU0123456789abcdefghijkl"]}),
        ])
        for operation in [lambda: client.search(""), lambda: client.channels.resolve(""),
                          lambda: client.channels.latest(),
                          lambda: client.channels.latest(channel="@TED", playlist="UU0123456789abcdefghijkl")]:
            with self.assertRaises(InvalidInput):
                operation()
        self.assertEqual(len(calls), 4)

    def test_library_scopes_coverage_and_monitor_route_encoding(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            parsed = urlparse(url)
            calls.append((method, parsed.path, parse_qs(parsed.query), json.loads(body) if body else None))
            return response(200, {"ok": True}, {"x-library-coverage": "37/412", "x-credits-charged": "0"})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport)
        client.library.coverage(playlist="PLfixture")
        passages = client.library.search('"solar energy" renew*', channel="@TED", limit=10, offset=20)
        self.assertEqual(passages.meta.library_coverage, "37/412")
        self.assertEqual(passages.meta.credits_charged, 0)
        self.assertNotIn("37/412", json.dumps(passages))
        client.monitors.create(channel="@TED", auto_fetch=False)
        client.monitors.list()
        client.monitors.get("id/with?delimiters")
        client.monitors.cancel("id/with?delimiters")
        self.assertEqual(calls, [
            ("GET", "/v1/library", {"playlist": ["PLfixture"]}, None),
            ("GET", "/v1/library/search", {"q": ['"solar energy" renew*'], "channel": ["@TED"], "limit": ["10"], "offset": ["20"]}, None),
            ("POST", "/v1/monitors", {}, {"channel": "@TED", "auto_fetch": False}),
            ("GET", "/v1/monitors", {}, None),
            ("GET", "/v1/monitors/id%2Fwith%3Fdelimiters", {}, None),
            ("DELETE", "/v1/monitors/id%2Fwith%3Fdelimiters", {}, None),
        ])
        for operation in [lambda: client.library.coverage(), lambda: client.library.search("word"),
                          lambda: client.library.search("word", channel="@TED", video_id="jNQXAC9IVRw"),
                          lambda: client.monitors.create(), lambda: client.monitors.get(""), lambda: client.monitors.cancel("")]:
            with self.assertRaises(InvalidInput):
                operation()
        self.assertEqual(len(calls), 6)

    def test_search_retry_cap_preserves_query_and_continuation_for_network_429_and_503(self):
        for max_retries in [0, 3, 8]:
            for failure in ["network", 429, 503]:
                with self.subTest(max_retries=max_retries, failure=failure):
                    calls, waits = [], []

                    def transport(method, url, headers, body, timeout):
                        calls.append((method, url, body))
                        if failure == "network":
                            raise OSError("search response lost")
                        return response(failure, {"errorClass": "QuotaExceeded" if failure == 429 else "RelayUnavailable",
                                                 "retryable": True, "message": "Try again.", "request_id": "search-final"},
                                        {"retry-after": "0"})

                    client = ScriptHaul("sh_test_unit_fixture", max_retries=max_retries,
                                       retry_delay=0, sleep=waits.append, transport=transport)
                    with self.assertRaises(ScriptHaulError) as caught:
                        client.search("science & art", type="video", channel="@TED", continuation="abc_-/+=")
                    if failure == "network":
                        self.assertIsInstance(caught.exception, ClientNetworkError)
                    else:
                        self.assertEqual(caught.exception.status, failure)
                        self.assertEqual(caught.exception.request_id, "search-final")
                    attempts = 1 if max_retries == 0 else 2
                    self.assertEqual(len(calls), attempts)
                    self.assertEqual(waits, [0] * (attempts - 1))
                    self.assertTrue(all(call == calls[0] for call in calls))
                    self.assertEqual(calls[0][0], "GET")
                    self.assertIsNone(calls[0][2])
                    parsed = urlparse(calls[0][1])
                    self.assertEqual(parsed.path, "/v1/search")
                    self.assertEqual(parse_qs(parsed.query), {"q": ["science & art"], "type": ["video"],
                                                            "channel": ["@TED"], "continuation": ["abc_-/+="]})

    def test_search_has_one_retry_budget_across_failure_kinds_and_honors_final_verdict(self):
        calls, waits = [], []

        def transport(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("search response lost")
            if len(calls) == 2:
                return response(503, {"errorClass": "RelayUnavailable", "retryable": True,
                                      "request_id": "second-attempt"}, {"retry-after": "0"})
            return response(200, {"ok": True, "results": []})

        client = ScriptHaul("sh_test_unit_fixture", max_retries=8, retry_delay=0,
                           transport=transport, sleep=waits.append)
        with self.assertRaises(RelayUnavailable) as caught:
            client.search("mixed failures")
        self.assertEqual(caught.exception.request_id, "second-attempt")
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [0])
        for status in [429, 503]:
            calls.clear()
            waits.clear()

            def final_transport(*args):
                calls.append(args)
                return response(status, {"errorClass": "QuotaExceeded" if status == 429 else "RelayUnavailable",
                                         "retryable": False, "request_id": "final-verdict"}, {"retry-after": "0"})

            final = ScriptHaul("sh_test_unit_fixture", max_retries=8, transport=final_transport, sleep=waits.append)
            with self.assertRaises(ScriptHaulError) as caught:
                final.search("final verdict")
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.request_id, "final-verdict")
            self.assertEqual(len(calls), 1)
            self.assertEqual(waits, [])

    def test_one_search_retry_succeeds_while_other_gets_keep_configured_allowance(self):
        calls = []

        def retry_transport(*args):
            calls.append(args)
            if len(calls) == 1:
                return response(503, {"errorClass": "RelayUnavailable", "retryable": True}, {"retry-after": "0"})
            return response(200, {"ok": True, "results": [{"video_id": "jNQXAC9IVRw"}]},
                            {"x-request-id": "search-replay", "x-credits-charged": "0"})

        client = ScriptHaul("sh_test_unit_fixture", max_retries=8, transport=retry_transport, sleep=lambda _: None)
        page = client.search("successful retry")
        self.assertEqual(len(calls), 2)
        self.assertEqual(page.meta.request_id, "search-replay")
        self.assertEqual(page.meta.credits_charged, 0)
        self.assertEqual(page["results"][0]["video_id"], "jNQXAC9IVRw")
        for kind in ["library", "listing"]:
            calls, waits = [], []

            def transport(method, url, *_):
                calls.append(urlparse(url).path)
                return response(503, {"errorClass": "RelayUnavailable", "retryable": True}, {"retry-after": "0"})

            client = ScriptHaul("sh_test_unit_fixture", max_retries=3, transport=transport, sleep=waits.append)
            with self.assertRaises(RelayUnavailable):
                if kind == "library":
                    client.library.search("energy", video_id="jNQXAC9IVRw")
                else:
                    client.videos("@TED")
            self.assertEqual(calls, ["/v1/library/search" if kind == "library" else "/v1/videos"] * 4)
            self.assertEqual(waits, [0, 0, 0])

    def test_retries_retryable_429_then_returns_listing(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            if len(calls) == 1:
                return response(429, {
                    "ok": False, "errorClass": "RateLimited", "code": "edge_rate_limit",
                    "message": "Slow down.", "retryable": True,
                    "request_id": "req-429",
                }, {"content-type": "application/json", "retry-after": "0"})
            return response(200, {"ok": True, "videos": []})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport, sleep=lambda _: None)
        self.assertEqual(client.videos(
            "https://youtube.com/@fixture", api_key="fixture-only")["videos"], [])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("api_key", calls[0])

    def test_final_429_is_raised_immediately_with_retry_after(self):
        calls = []
        waits = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            return response(429, {
                "ok": False, "errorClass": "QuotaExceeded", "code": "cold_fetch_cap",
                "message": "Daily cap.", "retryable": False, "retry_after": 86400,
                "request_id": "req-cap",
            }, {"content-type": "application/json", "retry-after": "86400"})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport, sleep=waits.append)
        with self.assertRaises(QuotaExceeded) as caught:
            client.videos("https://youtube.com/@fixture")
        self.assertEqual(caught.exception.code, "cold_fetch_cap")
        self.assertEqual(caught.exception.retry_after, 86400)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(calls), 1, "a final verdict is not retried")
        self.assertEqual(waits, [], "and the caller is never put to sleep")

    def test_long_retry_after_is_not_slept_through_unless_opted_in(self):
        calls = []
        waits = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            if len(calls) == 1:
                return response(503, {
                    "ok": False, "errorClass": "RelayUnavailable", "code": "relay_down",
                    "message": "Back soon.", "retryable": True, "request_id": "req-long",
                }, {"content-type": "application/json", "retry-after": "3600"})
            return response(200, {"ok": True, "videos": []})

        strict = ScriptHaul("sh_test_unit_fixture", transport=transport, sleep=waits.append)
        with self.assertRaises(RelayUnavailable) as caught:
            strict.videos("https://youtube.com/@fixture")
        self.assertEqual(caught.exception.retry_after, 3600)
        self.assertEqual(len(calls), 1)
        self.assertEqual(waits, [])

        calls.clear()
        patient = ScriptHaul("sh_test_unit_fixture", transport=transport,
                             sleep=waits.append, max_retry_after=7200)
        self.assertEqual(patient.videos("https://youtube.com/@fixture")["videos"], [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [3600.0])

    def test_exact_error_type_has_request_id(self):
        def transport(*_):
            return response(402, {
                "ok": False, "errorClass": "InsufficientCredits", "code": "shortfall",
                "message": "Buy more credits.", "retryable": False,
                "request_id": "req-credit-test", "shortfall": 2,
            })

        client = ScriptHaul("sh_test_unit_fixture", transport=transport)
        with self.assertRaises(InsufficientCredits) as caught:
            client.transcript("badcredits1")
        self.assertEqual(caught.exception.error_class, "InsufficientCredits")
        self.assertEqual(caught.exception.request_id, "req-credit-test")
        self.assertEqual(caught.exception.details["shortfall"], 2)

    def test_response_meta_carries_the_header_contract(self):
        def transport(method, url, headers, body, timeout):
            if "/v1/transcript" in url:
                return response(200, {"ok": True, "video": {"id": "dQw4w9WgXcQ"}, "text": "Fixture"}, {
                    "content-type": "application/json", "X-Cache": "HIT",
                    "X-Credits-Charged": "0", "X-Credits-Balance": "99",
                    "X-RateLimit-Limit": "60", "X-Request-Id": "req-meta-1",
                })
            if method == "POST":
                return response(202, {"ok": True, "job": {"id": "0123456789abcdef0123456789abcdef", "status": "pending"}}, {
                    "content-type": "application/json", "x-credits-balance": "97",
                    "x-request-id": "req-meta-2",
                })
            return job_body("completed", request_id="req-meta-3")

        client = ScriptHaul("sh_test_unit_fixture", transport=transport, sleep=lambda _: None)
        transcript = client.transcript("dQw4w9WgXcQ")
        self.assertIsInstance(transcript, ApiResponse)
        self.assertEqual(transcript, {"ok": True, "video": {"id": "dQw4w9WgXcQ"}, "text": "Fixture"})
        self.assertEqual(transcript.meta.cache, "HIT")
        self.assertEqual(transcript.meta.credits_charged, 0)
        self.assertEqual(transcript.meta.credits_balance, 99)
        self.assertEqual(transcript.meta.rate_limit, 60)
        self.assertEqual(transcript.meta.request_id, "req-meta-1")
        self.assertNotIn("meta", json.dumps(transcript))

        job = client.jobs.create(input="https://youtube.com/@fixture")
        self.assertEqual(job.meta.request_id, "req-meta-2")
        self.assertEqual(job.meta.credits_balance, 97)
        self.assertIsNone(job.meta.cache)
        job.refresh()
        self.assertEqual(job.meta.request_id, "req-meta-3")

    def test_wait_stops_on_a_paused_job(self):
        def paused(reason):
            def transport(method, url, headers, body, timeout):
                if method == "POST":
                    return response(202, {"ok": True, "job": {"id": "0123456789abcdef0123456789abcdef", "status": "pending"}})
                return job_body("paused", request_id="req-paused", pause_reason=reason)
            return ScriptHaul("sh_test_unit_fixture", transport=transport,
                              sleep=lambda _: None, job_poll_interval=0)

        short = paused("insufficient_credits").jobs.create(input="https://youtube.com/@fixture")
        with self.assertRaises(InsufficientCredits) as caught:
            short.wait()
        self.assertEqual(caught.exception.code, "job_paused_insufficient_credits")
        self.assertEqual(caught.exception.details["pause_reason"], "insufficient_credits")
        self.assertEqual(caught.exception.details["credits"]["outstanding"], 1)
        self.assertIs(caught.exception.job, short)
        self.assertEqual(caught.exception.request_id, "req-paused")

        outage = paused("proxy_budget").jobs.create(input="https://youtube.com/@fixture")
        with self.assertRaises(JobPaused) as caught:
            outage.wait()
        self.assertIsNone(caught.exception.error_class)
        self.assertEqual(caught.exception.code, "job_paused")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.details["pause_reason"], "proxy_budget")

        reads = []

        def transport(method, url, headers, body, timeout):
            if method == "POST":
                return response(202, {"ok": True, "job": {"id": "0123456789abcdef0123456789abcdef", "status": "pending"}})
            reads.append(url)
            if len(reads) < 3:
                return job_body("paused", pause_reason="insufficient_credits")
            return job_body("completed")

        resumed = ScriptHaul("sh_test_unit_fixture", transport=transport,
                             sleep=lambda _: None, job_poll_interval=0)
        job = resumed.jobs.create(input="https://youtube.com/@fixture")
        job.wait(wait_through_pauses=True)
        self.assertEqual(job.status, "completed")
        self.assertEqual(len(reads), 3)

    def test_cross_origin_poll_is_rejected_before_second_request(self):
        calls = []

        def transport(*args):
            calls.append(args[1])
            return response(202, {
                "ok": True, "request_id": "req-poll",
                "poll_url": "https://evil.example/poll",
            })

        client = ScriptHaul("sh_test_unit_fixture", transport=transport, sleep=lambda _: None)
        with self.assertRaises(InvalidInput) as caught:
            client.transcript("dQw4w9WgXcQ")
        self.assertEqual(caught.exception.code, "unsafe_poll_url")
        self.assertEqual(len(calls), 1)

    def test_http_redirect_never_forwards_the_bearer_key(self):
        received = []

        class QuietHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

        class TargetHandler(QuietHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        target_url = "http://127.0.0.1:%s/leak" % target.server_port

        class RedirectHandler(QuietHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target_url)
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            client = ScriptHaul(
                "sh_test_redirect_fixture",
                base_url="http://127.0.0.1:%s" % redirect.server_port,
                max_retries=0,
            )
            with self.assertRaises(ScriptHaulError):
                client.videos("https://youtube.com/@fixture")
            self.assertEqual(received, [])
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            redirect_thread.join()
            target_thread.join()

    def test_job_create_reuses_idempotency_key_on_retry(self):
        keys = []

        def transport(method, url, headers, body, timeout):
            keys.append(headers["Idempotency-Key"])
            if len(keys) == 1:
                return response(502, {
                    "ok": False, "errorClass": "UpstreamError", "code": "temporary",
                    "message": "Retry.", "retryable": True, "request_id": "req-first",
                }, {"content-type": "application/json", "retry-after": "0"})
            return response(202, {"ok": True, "job": {"id": "job-1", "status": "pending"}})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport, sleep=lambda _: None)
        job = client.jobs.create(input="https://youtube.com/@fixture")
        self.assertEqual(job.id, "job-1")
        self.assertEqual(len(keys), 2)
        self.assertTrue(keys[0])
        self.assertEqual(keys[0], keys[1])

    def test_monitor_creation_preserves_generated_and_supplied_keys_across_transport_and_http_retries(self):
        for supplied in [None, "Caller-Monitor_Key:+", "x" * 128]:
            with self.subTest(supplied=supplied):
                calls = []
                waits = []

                def transport(method, url, headers, body, timeout):
                    key = {name.lower(): value for name, value in headers.items()}.get("idempotency-key")
                    calls.append((key, urlparse(url), json.loads(body)))
                    if len(calls) == 1:
                        raise OSError("response lost")
                    if len(calls) == 2:
                        return response(502, {"ok": False, "errorClass": "UpstreamError", "message": "Retry.",
                                              "retryable": True}, {"content-type": "application/json", "retry-after": "0"})
                    return response(201, {"ok": True, "monitor": {"id": "monitor-fixture"}})

                client = ScriptHaul("sh_test_unit_fixture", transport=transport, retry_delay=0, sleep=waits.append)
                result = client.monitors.create(channel="@TED", auto_fetch=False, idempotency_key=supplied)
                self.assertEqual(result["monitor"]["id"], "monitor-fixture")
                self.assertEqual(len(calls), 3)
                self.assertTrue(calls[0][0])
                self.assertEqual(len({call[0] for call in calls}), 1)
                if supplied is not None:
                    self.assertEqual(calls[0][0], supplied)
                self.assertEqual(waits, [0, 0])
                for _, url, body in calls:
                    self.assertEqual(url.query, "")
                    self.assertEqual(body, {"channel": "@TED", "auto_fetch": False})
                client.monitors.create(channel="@TED")
                self.assertNotEqual(calls[3][0], calls[0][0])

    def test_invalid_monitor_idempotency_keys_never_reach_transport(self):
        calls = []

        def transport(*args):
            calls.append(args)
            return response(201, {"ok": True})

        client = ScriptHaul("sh_test_unit_fixture", transport=transport)
        for key in ["", " \t ", "bad\nkey", "bad\x00key", "x" * 129, 0, 42, False, {}, []]:
            with self.subTest(key=key), self.assertRaises(InvalidInput) as caught:
                client.monitors.create(channel="@TED", idempotency_key=key)
            self.assertEqual(caught.exception.code, "invalid_idempotency_key")
        self.assertEqual(calls, [])

    def test_post_retries_require_valid_case_insensitive_idempotency_header(self):
        for failure in ["network", "http"]:
            for headers in [None, {}, {"Idempotency-Key": ""}, {"IDEMPOTENCY-KEY": "  "},
                            {"idempotency-key": "x" * 129}, {"Idempotency-Key": "one", "idempotency-key": "two"}]:
                with self.subTest(failure=failure, headers=headers):
                    calls = []
                    waits = []

                    def transport(*args):
                        calls.append(args)
                        if failure == "network":
                            raise OSError("response lost")
                        return response(502, {"ok": False, "errorClass": "UpstreamError", "message": "Retry.", "retryable": True})

                    client = ScriptHaul("sh_test_unit_fixture", transport=transport, sleep=waits.append)
                    with self.assertRaises(ScriptHaulError):
                        client._request("POST", "/v1/jobs/job-fixture/cancel", headers=headers)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(waits, [])
            calls = []

            def retry_transport(*args):
                calls.append(args)
                if len(calls) == 1:
                    if failure == "network":
                        raise OSError("response lost")
                    return response(502, {"ok": False, "errorClass": "UpstreamError", "message": "Retry.", "retryable": True})
                return response(200, {"ok": True})

            client = ScriptHaul("sh_test_unit_fixture", transport=retry_transport, sleep=lambda _: None)
            client._request("POST", "/v1/monitors", headers={"iDeMpOtEnCy-KeY": "case-sensitive-value"}, body={"channel": "@TED"})
            self.assertEqual(len(calls), 2)

    def test_client_side_failures_never_carry_a_server_error_class(self):
        def offline(*_):
            raise OSError("ENOTFOUND api.scripthaul.com")

        client = ScriptHaul("sh_test_unit_fixture", transport=offline,
                            sleep=lambda _: None, max_retries=1)
        with self.assertRaises(ClientNetworkError) as caught:
            client.videos("https://youtube.com/@fixture")
        self.assertIsInstance(caught.exception, ScriptHaulError)
        self.assertNotIsInstance(caught.exception, RelayUnavailable)
        self.assertIsNone(caught.exception.error_class)
        self.assertEqual(caught.exception.code, "network_error")
        self.assertTrue(caught.exception.retryable)
        self.assertIn("ENOTFOUND", caught.exception.details["cause"])

        clock = [0.0]

        def running(method, url, headers, body, timeout):
            if method == "POST":
                return response(202, {"ok": True, "job": {"id": "0123456789abcdef0123456789abcdef", "status": "pending"}})
            clock[0] += 1.0
            return job_body("running")

        stuck = ScriptHaul("sh_test_unit_fixture", transport=running, sleep=lambda _: None,
                           job_poll_interval=0, job_timeout=0.5, monotonic=lambda: clock[0])
        job = stuck.jobs.create(input="https://youtube.com/@fixture")
        with self.assertRaises(ClientTimeout) as caught:
            job.wait()
        self.assertIsNone(caught.exception.error_class)
        self.assertEqual(caught.exception.code, "job_wait_timeout")

    def test_202_polling_stops_at_the_configured_bound(self):
        calls = []

        def transport(*args):
            calls.append(args[1])
            return response(202, {
                "ok": True, "request_id": "req-poll-bound",
                "poll_url": "/v1/transcript?request_id=req-poll-bound",
            }, {"content-type": "application/json", "retry-after": "0"})

        client = ScriptHaul(
            "sh_test_unit_fixture", transport=transport, sleep=lambda _: None,
            max_polls=1,
        )
        with self.assertRaises(ClientTimeout) as caught:
            client.transcript("dQw4w9WgXcQ")
        self.assertIsNone(caught.exception.error_class)
        self.assertEqual(caught.exception.code, "poll_timeout")
        self.assertEqual(caught.exception.request_id, "req-poll-bound")
        self.assertEqual(len(calls), 2)

    def test_version_header_matches_pyproject(self):
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        declared = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE).group(1)
        self.assertEqual(scripthaul.__version__, declared)
        self.assertEqual(SDK_HEADER, "python/%s" % declared)
        sent = {}

        def transport(method, url, headers, body, timeout):
            sent.update(headers)
            return response(200, {"ok": True, "videos": []})

        ScriptHaul("sh_test_unit_fixture", transport=transport).videos("https://youtube.com/@fixture")
        self.assertEqual(sent["X-ScriptHaul-SDK"], SDK_HEADER)

    def test_api_key_never_appears_in_repr_str_or_vars(self):
        key = "sh_test_unit_fixture_secret_value_0123456789"
        client = ScriptHaul(key, transport=lambda *_: response(200, {}))
        self.assertEqual(client.api_key, key)
        self.assertNotIn(key, repr(client))
        self.assertNotIn(key, str(client))
        self.assertIn("sh_test_unit...", repr(client))
        self.assertNotIn(key, repr(vars(client)))
        self.assertNotIn(key, repr(client.jobs.__dict__))


if __name__ == "__main__":
    unittest.main()
