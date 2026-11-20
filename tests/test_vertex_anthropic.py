# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the loopback Anthropic-to-Vertex proxy, with a fake upstream in place of Vertex AI."""

import fcntl
import http.client
import json
import logging
import socket
import struct
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlparse

import google.auth.exceptions
import pytest
import requests
from pytest_mock import MockerFixture

from review_assistant import vertex_anthropic
from review_assistant.vertex_anthropic import (
    CLOUD_PLATFORM_SCOPE,
    TokenCache,
    VertexAnthropicConfig,
    VertexAnthropicProxy,
    VertexProxyError,
    normalize_model,
    predict_url,
)

MESSAGE: dict[str, object] = {
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
}
ANSWER = {"type": "message", "content": [{"type": "text", "text": "OK"}]}
JSON_ANSWER = (200, "application/json", [json.dumps(ANSWER).encode()])


@dataclass
class _Received:
    path: str
    headers: dict[str, str]
    body: dict[str, object]


class _UpstreamServer(ThreadingHTTPServer):
    responses: list[tuple[int, str, Sequence[bytes]]]
    requests: list[_Received]


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        server = cast("_UpstreamServer", self.server)
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        server.requests.append(_Received(self.path, dict(self.headers), json.loads(raw)))
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
        return f"http://127.0.0.1:{self._server.server_address[1]}/publishers/anthropic/models"

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


def _tokens(adc: str = "adc-token", *, lifetime: timedelta = timedelta(hours=1)) -> TokenCache:
    return TokenCache(lambda: (adc, datetime.now(UTC) + lifetime))


@contextmanager
def _running(base_url: str, tokens: TokenCache) -> Iterator[str]:
    config = VertexAnthropicConfig(project="vertex-ai-206179", base_url=base_url)
    with VertexAnthropicProxy(config, tokens) as proxy:
        yield proxy.url


def _header(received: _Received, name: str) -> str | None:
    return next((value for key, value in received.headers.items() if key.lower() == name.lower()), None)


_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1
_LOOPBACK = "lo"
_IFREQ = "16sH" + "14x"


@pytest.fixture(autouse=True)
def _loopback_is_up() -> None:
    """Enable loopback, which the network namespace of `make test` (unshare -n) leaves down."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack(_IFREQ, _LOOPBACK.encode(), 0)
        flags = struct.unpack("H", fcntl.ioctl(sock, _SIOCGIFFLAGS, request)[16:18])[0]
        if not flags & _IFF_UP:
            fcntl.ioctl(sock, _SIOCSIFFLAGS, struct.pack(_IFREQ, _LOOPBACK.encode(), flags | _IFF_UP))


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        pytest.param("claude-haiku-5-5", "claude-haiku-5-5@default", id="bare-model-gets-default-version"),
        pytest.param("anthropic/claude-haiku-5-5", "claude-haiku-5-5@default", id="anthropic-prefix-is-stripped"),
        pytest.param("claude-haiku-5-5@default", "claude-haiku-5-5@default", id="default-version-is-not-doubled"),
        pytest.param("claude-haiku-5-5@20260101", "claude-haiku-5-5@20260101", id="dated-version-is-kept"),
    ],
)
def test_normalize_model(model: str, expected: str) -> None:
    assert normalize_model(model) == expected


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({"GOOGLE_CLOUD_PROJECT": "p"}, VertexAnthropicConfig("p"), id="google-cloud-project"),
        pytest.param({"GOOGLE_VERTEX_PROJECT": "p"}, VertexAnthropicConfig("p"), id="google-vertex-project"),
        pytest.param({"VERTEX_PROJECT_ID": "p"}, VertexAnthropicConfig("p"), id="vertex-project-id"),
        pytest.param(
            {"GOOGLE_CLOUD_PROJECT": "", "VERTEX_PROJECT_ID": "p"},
            VertexAnthropicConfig("p"),
            id="empty-project-falls-through",
        ),
        pytest.param(
            {"GOOGLE_CLOUD_PROJECT": "p", "GOOGLE_CLOUD_LOCATION": "europe-west1"},
            VertexAnthropicConfig("p", "europe-west1"),
            id="google-cloud-location",
        ),
        pytest.param(
            {"GOOGLE_CLOUD_PROJECT": "p", "GOOGLE_VERTEX_LOCATION": "us"},
            VertexAnthropicConfig("p", "us"),
            id="google-vertex-location",
        ),
    ],
)
def test_config_from_env(env: dict[str, str], expected: VertexAnthropicConfig) -> None:
    assert VertexAnthropicConfig.from_env(env) == expected


def test_config_from_env_reads_the_process_environment(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "vertex-ai-206179"}, clear=True)
    assert VertexAnthropicConfig.from_env() == VertexAnthropicConfig("vertex-ai-206179")


def test_config_from_env_needs_a_project() -> None:
    with pytest.raises(VertexProxyError, match="GOOGLE_CLOUD_PROJECT or VERTEX_PROJECT_ID is not set"):
        VertexAnthropicConfig.from_env({"GOOGLE_CLOUD_LOCATION": "global"})


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param(
            VertexAnthropicConfig("p"),
            "https://aiplatform.googleapis.com/v1/projects/p/locations/global/publishers/anthropic/models/"
            "claude-haiku-5-5@default:rawPredict",
            id="global-raw-predict",
        ),
        pytest.param(
            VertexAnthropicConfig("p", "us"),
            "https://aiplatform.us.rep.googleapis.com/v1/projects/p/locations/us/publishers/anthropic/models/"
            "claude-haiku-5-5@default:rawPredict",
            id="us-rep-endpoint",
        ),
        pytest.param(
            VertexAnthropicConfig("p", "europe-west1"),
            "https://europe-west1-aiplatform.googleapis.com/v1/projects/p/locations/europe-west1/"
            "publishers/anthropic/models/claude-haiku-5-5@default:rawPredict",
            id="regional-endpoint",
        ),
        pytest.param(
            VertexAnthropicConfig("p", base_url="http://127.0.0.1:1/models/"),
            "http://127.0.0.1:1/models/claude-haiku-5-5@default:rawPredict",
            id="base-url-override",
        ),
    ],
)
def test_predict_url(config: VertexAnthropicConfig, expected: str) -> None:
    assert predict_url(config, "claude-haiku-5-5", stream=False) == expected


def test_predict_url_streaming_uses_stream_raw_predict() -> None:
    expected = (
        "https://aiplatform.googleapis.com/v1/projects/p/locations/global/publishers/anthropic/models/"
        "claude-haiku-5-5@default:streamRawPredict"
    )
    assert predict_url(VertexAnthropicConfig("p"), "claude-haiku-5-5", stream=True) == expected


def test_proxy_rewrites_an_anthropic_call_to_vertex(serve: _Serve) -> None:
    upstream = serve([JSON_ANSWER])
    with _running(upstream.url, _tokens()) as url:
        response = requests.post(
            f"{url}/v1/messages",
            json={**MESSAGE, "model": "anthropic/claude-haiku-5-5"},
            headers={"x-api-key": "vertex-proxy", "anthropic-version": "2023-06-01", "anthropic-beta": "tools-2024"},
            timeout=10,
        )

    assert response.status_code == 200
    assert response.json() == ANSWER
    received = upstream.requests[0]
    assert received.path == "/publishers/anthropic/models/claude-haiku-5-5@default:rawPredict"
    assert received.body == {**MESSAGE, "anthropic_version": "vertex-2023-10-16"}
    assert "model" not in received.body
    assert _header(received, "Authorization") == "Bearer adc-token"
    assert _header(received, "Content-Type") == "application/json"
    assert _header(received, "x-api-key") is None
    assert _header(received, "anthropic-version") is None
    assert _header(received, "anthropic-beta") is None


def test_proxy_streams_upstream_chunks(serve: _Serve) -> None:
    chunks = [b"event: message_start\n\ndata: {}\n\n", b"data: [DONE]\n\n"]
    upstream = serve([(200, "text/event-stream", chunks)])
    with _running(upstream.url, _tokens()) as url:
        response = requests.post(
            f"{url}/v1/messages",
            json={**MESSAGE, "model": "claude-haiku-5-5", "stream": True},
            stream=True,
            timeout=10,
        )
        body = b"".join(response.iter_content())

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/event-stream"
    assert body == b"".join(chunks)
    assert upstream.requests[0].path.endswith("/claude-haiku-5-5@default:streamRawPredict")


def test_proxy_relays_an_empty_vertex_body(serve: _Serve) -> None:
    upstream = serve([(200, "application/json", [])])
    with _running(upstream.url, _tokens()) as url:
        response = requests.post(f"{url}/v1/messages", json={**MESSAGE, "model": "claude-haiku-5-5"}, timeout=10)

    assert response.status_code == 200
    assert response.content == b""


def test_proxy_relays_a_vertex_error(serve: _Serve) -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad request"}}
    upstream = serve([(400, "application/json", [json.dumps(error).encode()])])
    with _running(upstream.url, _tokens()) as url:
        response = requests.post(f"{url}/v1/messages", json={**MESSAGE, "model": "claude-haiku-5-5"}, timeout=10)

    assert response.status_code == 400
    assert response.json() == error


def test_proxy_rejects_other_paths(serve: _Serve) -> None:
    upstream = serve([])
    with _running(upstream.url, _tokens()) as url:
        response = requests.post(f"{url}/v1/complete", json=MESSAGE, timeout=10)

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "only POST /v1/messages is proxied"
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(b"not json", "request body is not JSON", id="body-is-not-json"),
        pytest.param(b"null", "must be a JSON object", id="body-is-not-an-object"),
        pytest.param(b"{}", "must be a JSON object", id="model-is-missing"),
        pytest.param(b'{"model": 5}', "must be a JSON object", id="model-is-not-a-string"),
        pytest.param(b'{"model": ""}', "must be a JSON object", id="model-is-empty"),
    ],
)
def test_proxy_rejects_malformed_bodies(serve: _Serve, body: bytes, expected: str) -> None:
    upstream = serve([])
    with _running(upstream.url, _tokens()) as url:
        response = requests.post(
            f"{url}/v1/messages", data=body, headers={"Content-Type": "application/json"}, timeout=10
        )

    assert response.status_code == 400
    assert expected in response.json()["error"]["message"]
    assert upstream.requests == []


def test_proxy_treats_an_invalid_content_length_as_empty(serve: _Serve) -> None:
    upstream = serve([])
    with _running(upstream.url, _tokens()) as url:
        connection = http.client.HTTPConnection("127.0.0.1", urlparse(url).port, timeout=10)
        connection.putrequest("POST", "/v1/messages")
        connection.putheader("Content-Length", "many")
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

    assert response.status == 400
    assert payload["error"]["message"] == "request body is not JSON"


def test_proxy_reports_an_unreachable_vertex() -> None:
    config = VertexAnthropicConfig(project="vertex-ai-206179", base_url="http://127.0.0.1:1/models")
    with VertexAnthropicProxy(config, _tokens()) as proxy:
        response = requests.post(f"{proxy.url}/v1/messages", json={**MESSAGE, "model": "claude-haiku-5-5"}, timeout=10)

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "vertex request failed"


def test_proxy_reports_missing_adc_credentials(serve: _Serve) -> None:
    def fail() -> tuple[str, datetime]:
        msg = "no credentials"
        raise VertexProxyError(msg)

    upstream = serve([])
    with _running(upstream.url, TokenCache(fail)) as url:
        response = requests.post(f"{url}/v1/messages", json={**MESSAGE, "model": "claude-haiku-5-5"}, timeout=10)

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Google ADC access token unavailable"
    assert upstream.requests == []


def test_proxy_never_logs_the_access_token(serve: _Serve, caplog: pytest.LogCaptureFixture) -> None:
    secret = "adc-super-secret-token"
    upstream = serve([JSON_ANSWER])
    with (
        caplog.at_level(logging.DEBUG, logger="review_assistant.vertex_anthropic"),
        _running(upstream.url, _tokens(secret)) as url,
    ):
        response = requests.post(f"{url}/v1/messages", json={**MESSAGE, "model": "claude-haiku-5-5"}, timeout=10)
        response.raise_for_status()

    assert secret not in caplog.text
    assert _header(upstream.requests[0], "Authorization") == f"Bearer {secret}"


def test_proxy_url_requires_a_running_proxy() -> None:
    proxy = VertexAnthropicProxy(VertexAnthropicConfig("p"))
    with pytest.raises(VertexProxyError, match="not running"):
        _ = proxy.url


def test_proxy_start_and_stop_are_idempotent() -> None:
    proxy = VertexAnthropicProxy(VertexAnthropicConfig("p"))
    proxy.stop()
    url = proxy.start().url
    assert urlparse(url).hostname == "127.0.0.1"
    assert proxy.start().url == url
    proxy.stop()
    proxy.stop()
    with pytest.raises(VertexProxyError, match="not running"):
        _ = proxy.url


def test_token_cache_reuses_a_live_token(mocker: MockerFixture) -> None:
    fetch = mocker.Mock(return_value=("token-1", datetime.now(UTC) + timedelta(hours=1)))
    cache = TokenCache(fetch)
    assert [cache.token(), cache.token()] == ["token-1", "token-1"]
    fetch.assert_called_once_with()


def test_token_cache_refreshes_an_expired_token(mocker: MockerFixture) -> None:
    fetch = mocker.Mock(
        side_effect=[("old", datetime.now(UTC) - timedelta(seconds=1)), ("new", datetime.now(UTC) + timedelta(hours=1))]
    )
    cache = TokenCache(fetch)
    assert [cache.token(), cache.token()] == ["old", "new"]
    assert fetch.call_count == 2


def test_token_cache_refreshes_within_the_expiry_margin(mocker: MockerFixture) -> None:
    fetch = mocker.Mock(return_value=("short", datetime.now(UTC) + timedelta(minutes=1)))
    cache = TokenCache(fetch)
    assert [cache.token(), cache.token()] == ["short", "short"]
    assert fetch.call_count == 2


def test_token_cache_reads_adc(mocker: MockerFixture) -> None:
    credentials = mocker.Mock(token="adc-token", expiry=datetime.now(UTC) + timedelta(hours=1))
    default = mocker.patch.object(vertex_anthropic.google.auth, "default", return_value=(credentials, "project"))
    cache = TokenCache()
    assert [cache.token(), cache.token()] == ["adc-token", "adc-token"]
    default.assert_called_once_with(scopes=[CLOUD_PLATFORM_SCOPE])
    credentials.refresh.assert_called_once()


def test_token_cache_accepts_a_naive_expiry(mocker: MockerFixture) -> None:
    credentials = mocker.Mock(token="adc-token", expiry=datetime(2027, 1, 1))  # noqa: DTZ001 - ADC may return naive expiry
    mocker.patch.object(vertex_anthropic.google.auth, "default", return_value=(credentials, "project"))
    assert TokenCache().token() == "adc-token"


def test_token_cache_defaults_the_expiry_to_one_hour(mocker: MockerFixture) -> None:
    credentials = mocker.Mock(token="adc-token", expiry=None)
    mocker.patch.object(vertex_anthropic.google.auth, "default", return_value=(credentials, "project"))
    assert TokenCache().token() == "adc-token"


def test_token_cache_reports_missing_google_credentials(mocker: MockerFixture) -> None:
    failure = google.auth.exceptions.DefaultCredentialsError("no adc")
    mocker.patch.object(vertex_anthropic.google.auth, "default", side_effect=failure)
    with pytest.raises(VertexProxyError, match="cannot obtain a Google ADC access token"):
        TokenCache().token()


def test_token_cache_reports_an_empty_access_token(mocker: MockerFixture) -> None:
    credentials = mocker.Mock(token=None, expiry=None)
    mocker.patch.object(vertex_anthropic.google.auth, "default", return_value=(credentials, "project"))
    with pytest.raises(VertexProxyError, match="no access token"):
        TokenCache().token()
