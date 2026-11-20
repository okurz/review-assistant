# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the loopback inference proxy, with a fake OpenAI-compatible upstream in place of Helmcode."""

import fcntl
import http.client
import json
import logging
import socket
import struct
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlparse

import pytest
from pytest_mock import MockerFixture

from review_assistant.config import ConfigError
from review_assistant.inference_proxy import (
    API_KEY_ENV,
    API_PATH,
    BASE_URL_ENV,
    SENTINEL_API_KEY,
    InferenceProxy,
    InferenceProxyError,
    inference_env,
)

KEY = "sk-" + "not-a-real-key" * 3
CHAT = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "stream": False}
ANSWER = {"id": "chatcmpl-1", "choices": [{"message": {"content": "OK"}}]}
JSON_ANSWER = (200, "application/json", [json.dumps(ANSWER).encode()])
STREAM = (200, "text/event-stream", [b'data: {"delta":"a"}\n\n', b'data: {"delta":"b"}\n\n', b"data: [DONE]\n\n"])


@dataclass
class _Received:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class _UpstreamServer(ThreadingHTTPServer):
    responses: list[tuple[int, str, Sequence[bytes]]]
    requests: list[_Received]


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._record("GET")

    def do_POST(self) -> None:
        self._record("POST")

    def _record(self, method: str) -> None:
        server = cast("_UpstreamServer", self.server)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        server.requests.append(_Received(method, self.path, dict(self.headers), body))
        status, content_type, chunks = server.responses.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for chunk in chunks:
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - BaseHTTPRequestHandler signature
        pass


class _Upstream:
    def __init__(self, responses: Sequence[tuple[int, str, Sequence[bytes]]]) -> None:
        self._server = _UpstreamServer(("127.0.0.1", 0), _UpstreamHandler)
        self._server.responses = list(responses)
        self._server.requests = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "_Upstream":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{API_PATH}"

    @property
    def requests(self) -> list[_Received]:
        return self._server.requests


type _Responses = Sequence[tuple[int, str, Sequence[bytes]]]
type _Serve = Callable[[_Responses], _Upstream]


@pytest.fixture
def serve() -> Iterator[_Serve]:
    started: list[_Upstream] = []

    def start(responses: _Responses) -> _Upstream:
        upstream = _Upstream(responses).start()
        started.append(upstream)
        return upstream

    yield start
    for upstream in started:
        upstream.stop()


@pytest.fixture(autouse=True)
def _loopback_is_up() -> None:
    """Enable loopback, which the network namespace of `make test` (unshare -n) leaves down."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("16sH" + "14x", b"lo", 0)
        flags = struct.unpack("H", fcntl.ioctl(sock, 0x8913, request)[16:18])[0]
        if not flags & 0x1:
            fcntl.ioctl(sock, 0x8914, struct.pack("16sH" + "14x", b"lo", flags | 0x1))


def _call(
    url: str, method: str = "POST", body: Mapping[str, object] | None = None
) -> tuple[int, dict[str, str], bytes]:
    parsed = urlparse(url)
    connection = http.client.HTTPConnection(parsed.hostname or "", parsed.port or 80, timeout=30)
    target = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
    try:
        payload = json.dumps(body).encode() if body is not None else None
        connection.request(method, target, body=payload, headers={"Authorization": f"Bearer {SENTINEL_API_KEY}"})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _header(received: _Received, name: str) -> str | None:
    return next((value for key, value in received.headers.items() if key.lower() == name), None)


@pytest.mark.parametrize(
    ("proxy_url", "expected"),
    [
        pytest.param(None, {}, id="no-proxy-no-variables"),
        pytest.param(
            "http://127.0.0.1:8000/v1",
            {BASE_URL_ENV: "http://127.0.0.1:8000/v1", API_KEY_ENV: SENTINEL_API_KEY},
            id="proxy-url-and-sentinel",
        ),
    ],
)
def test_inference_env_points_hermes_at_the_proxy(proxy_url: str | None, expected: dict[str, str]) -> None:
    assert inference_env(proxy_url) == expected


def test_chat_completion_is_forwarded_with_the_real_key(serve: _Serve) -> None:
    upstream = serve([JSON_ANSWER])
    with InferenceProxy(upstream.url, KEY) as proxy:
        status, headers, body = _call(f"{proxy.url}/chat/completions", body=CHAT)
    assert (status, json.loads(body)) == (200, ANSWER)
    assert headers["Content-Type"] == "application/json"
    received = upstream.requests[0]
    assert (received.method, received.path) == ("POST", f"{API_PATH}/chat/completions")
    assert json.loads(received.body) == CHAT
    assert _header(received, "authorization") == f"Bearer {KEY}"
    assert SENTINEL_API_KEY not in json.dumps(received.headers)


def test_model_listing_is_forwarded_without_a_body(serve: _Serve) -> None:
    upstream = serve([(200, "application/json", [b'{"data":[]}'])])
    with InferenceProxy(upstream.url, KEY) as proxy:
        status, _headers, body = _call(f"{proxy.url}/models", method="GET")
    assert (status, body) == (200, b'{"data":[]}')
    assert upstream.requests[0].method == "GET"


def test_a_path_outside_v1_is_refused_without_contacting_the_upstream(serve: _Serve) -> None:
    upstream = serve([JSON_ANSWER])
    with InferenceProxy(upstream.url, KEY) as proxy:
        status, headers, body = _call(f"http://{urlparse(proxy.url).netloc}/admin", method="GET")
    assert (status, headers["Content-Type"]) == (404, "application/json")
    assert "only /v1/" in body.decode()
    assert upstream.requests == []


def test_upstream_error_passes_through_unchanged(serve: _Serve) -> None:
    upstream = serve([(429, "application/json", [b'{"error":"quota"}'])])
    with InferenceProxy(upstream.url, KEY) as proxy:
        status, _headers, body = _call(f"{proxy.url}/chat/completions", body=CHAT)
    assert (status, body) == (429, b'{"error":"quota"}')


def test_a_streamed_answer_keeps_its_chunks_and_framing(serve: _Serve) -> None:
    upstream = serve([STREAM])
    with InferenceProxy(upstream.url, KEY) as proxy:
        status, headers, body = _call(f"{proxy.url}/chat/completions", body=CHAT | {"stream": True})
    assert status == 200
    assert headers["Content-Type"] == "text/event-stream"
    assert body == b"".join(STREAM[2])
    assert headers["Transfer-Encoding"] == "chunked"


def test_an_unreachable_upstream_is_a_bad_gateway(caplog: pytest.LogCaptureFixture) -> None:
    with InferenceProxy("http://127.0.0.1:1/v1", KEY) as proxy:
        status, _headers, body = _call(f"{proxy.url}/chat/completions", body=CHAT)
    assert status == 502
    assert "failed" in body.decode()
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    "api_key",
    [pytest.param("", id="empty"), pytest.param(" ", id="blank")],
)
def test_a_missing_key_is_a_configuration_error(api_key: str) -> None:
    with pytest.raises(ConfigError, match="inference API key is missing"):
        InferenceProxy("https://api.example.com/v1", api_key.strip())


def test_a_stopped_or_unstarted_proxy_has_no_url() -> None:
    proxy_instance = InferenceProxy("https://api.example.com/v1", KEY)
    with pytest.raises(InferenceProxyError, match="not running"):
        assert proxy_instance.url
    with proxy_instance as started:
        assert started.url.startswith("http://127.0.0.1:")
        assert started.url.endswith(API_PATH)
    with pytest.raises(InferenceProxyError, match="not running"):
        assert proxy_instance.url


def test_the_proxy_starts_once_and_stops_idempotently() -> None:
    proxy_instance = InferenceProxy("https://api.example.com/v1", KEY)
    first = proxy_instance.start()
    assert first.start() is first
    first.stop()
    first.stop()


def test_the_key_never_reaches_a_log_record(mocker: MockerFixture) -> None:
    info = mocker.patch.object(logging.getLogger("review_assistant.inference_proxy"), "info")
    with InferenceProxy("https://api.example.com/v1", KEY) as proxy_instance:
        assert proxy_instance.url
    assert info.called
    assert all(KEY not in str(call) for call in info.call_args_list)
