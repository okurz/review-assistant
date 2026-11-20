# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Loopback proxy that adapts Anthropic Messages calls to Google Vertex AI Anthropic rawPredict.

Hermes' ``anthropic`` provider speaks the Anthropic Messages API and honors ``ANTHROPIC_BASE_URL``, but it
cannot sign requests for Vertex AI. The proxy keeps Google ADC the only credential: it rewrites each
``POST /v1/messages`` into a Vertex ``rawPredict``/``streamRawPredict`` call and relays the answer, including
streaming chunks, unchanged.
"""

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self, cast, override
from urllib.parse import urlsplit

import google.auth
import google.auth.exceptions
import google.auth.transport.requests
import requests

MESSAGES_PATH = "/v1/messages"
VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
DEFAULT_LOCATION = "global"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
LOOPBACK_HOST = "127.0.0.1"
_TOKEN_MARGIN = timedelta(minutes=5)
_UPSTREAM_TIMEOUT = 600.0
_READ_CHUNK = 8192
_END_OF_CHUNKS = b"0\r\n\r\n"
_STRIPPED_HEADERS = frozenset({
    "anthropic-version",
    "authorization",
    "connection",
    "content-length",
    "host",
    "x-api-key",
})

log = logging.getLogger(__name__)


class VertexProxyError(RuntimeError):
    """The Vertex Anthropic proxy is misconfigured or cannot get a Google ADC access token."""


def normalize_model(model: str) -> str:
    """Map an Anthropic model name to its Vertex publisher id, e.g. ``claude-haiku-5-5@default``."""
    name = model.strip().removeprefix("anthropic/")
    return name if "@" in name else f"{name}@default"


def _vertex_host(location: str) -> str:
    if location == "global":
        return "aiplatform.googleapis.com"
    if location in {"eu", "us"}:
        return f"aiplatform.{location}.rep.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"


@dataclass(frozen=True)
class VertexAnthropicConfig:
    """The Vertex AI project and location whose Anthropic publisher models are called."""

    project: str
    location: str = DEFAULT_LOCATION
    base_url: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the project and location from the environment, failing fast when the project is absent."""
        values = os.environ if env is None else env
        project = (
            values.get("GOOGLE_CLOUD_PROJECT") or values.get("GOOGLE_VERTEX_PROJECT") or values.get("VERTEX_PROJECT_ID")
        )
        if not project:
            msg = "GOOGLE_CLOUD_PROJECT or VERTEX_PROJECT_ID is not set; the Vertex Anthropic proxy needs a GCP project"
            raise VertexProxyError(msg)
        location = values.get("GOOGLE_CLOUD_LOCATION") or values.get("GOOGLE_VERTEX_LOCATION") or DEFAULT_LOCATION
        return cls(project=project, location=location)

    @property
    def model_base(self) -> str:
        """Base URL of the publisher models; ``base_url`` overrides it so tests can use a fake upstream."""
        if self.base_url is not None:
            return self.base_url.rstrip("/")
        return (
            f"https://{_vertex_host(self.location)}/v1/projects/{self.project}"
            f"/locations/{self.location}/publishers/anthropic/models"
        )


def predict_url(config: VertexAnthropicConfig, model: str, *, stream: bool) -> str:
    """Return the Vertex ``rawPredict`` or ``streamRawPredict`` URL of model."""
    action = "streamRawPredict" if stream else "rawPredict"
    return f"{config.model_base}/{normalize_model(model)}:{action}"


def _refresh_access_token() -> tuple[str, datetime]:
    try:
        credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        credentials.refresh(google.auth.transport.requests.Request())
    except (google.auth.exceptions.GoogleAuthError, OSError) as error:
        msg = f"cannot obtain a Google ADC access token: {error}"
        raise VertexProxyError(msg) from None
    if not credentials.token:
        msg = "Google ADC returned no access token"
        raise VertexProxyError(msg)
    expiry = credentials.expiry or datetime.now(UTC) + timedelta(hours=1)
    return credentials.token, expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry


class TokenCache:
    """Google ADC access token, refreshed only once the cached one nears expiry."""

    def __init__(self, fetch: Callable[[], tuple[str, datetime]] | None = None) -> None:
        """Cache the token returned by fetch, by default the one Google ADC hands out."""
        self._fetch = fetch or _refresh_access_token
        self._token = ""
        self._expires_at: datetime | None = None

    def token(self) -> str:
        """Return a valid access token, fetching a new one when the cached one expires within the margin."""
        expires_at = self._expires_at
        if expires_at is None or expires_at - _TOKEN_MARGIN <= datetime.now(UTC):
            self._token, self._expires_at = self._fetch()
        return self._token


def _forward_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers
        if name.lower() not in _STRIPPED_HEADERS and not name.lower().startswith("anthropic-beta")
    }


class _VertexServer(ThreadingHTTPServer):
    """Loopback-only HTTP server carrying the proxy configuration and the token cache."""

    config: VertexAnthropicConfig
    tokens: TokenCache
    upstream_timeout: float


class _VertexHandler(BaseHTTPRequestHandler):
    """Relay ``POST /v1/messages`` to Vertex and stream the upstream answer back."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        server = cast("_VertexServer", self.server)
        if urlsplit(self.path).path != MESSAGES_PATH:
            self._reply(HTTPStatus.NOT_FOUND, "only POST /v1/messages is proxied")
            return
        parsed = self._parse_body()
        if parsed is None:
            return
        model, payload = parsed
        try:
            token = server.tokens.token()
        except VertexProxyError as error:
            log.warning("vertex credentials unavailable: %s", error)
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, "Google ADC access token unavailable")
            return
        payload["anthropic_version"] = VERTEX_ANTHROPIC_VERSION
        url = predict_url(server.config, model, stream=bool(payload.get("stream")))
        headers = {**_forward_headers(self.headers.items()), "Authorization": f"Bearer {token}"}
        try:
            upstream = requests.post(url, json=payload, headers=headers, stream=True, timeout=server.upstream_timeout)
        except requests.RequestException as error:
            log.warning("vertex request to %s failed: %s", model, error.__class__.__name__)
            self._reply(HTTPStatus.BAD_GATEWAY, "vertex request failed")
            return
        with upstream:
            self._relay(upstream)

    def _parse_body(self) -> tuple[str, dict[str, object]] | None:
        try:
            payload = json.loads(self.rfile.read(self._content_length()))
        except json.JSONDecodeError:
            self._reply(HTTPStatus.BAD_REQUEST, "request body is not JSON")
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or not payload["model"]:
            self._reply(HTTPStatus.BAD_REQUEST, "request body must be a JSON object with a non-empty string model")
            return None
        model = cast("str", payload.pop("model"))
        return model, cast("dict[str, object]", payload)

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return 0

    def _relay(self, upstream: requests.Response) -> None:
        content_type = upstream.headers.get("Content-Type", "application/json")
        self.send_response(upstream.status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for chunk in filter(None, upstream.iter_content(chunk_size=_READ_CHUNK)):
            self._write_chunk(chunk)
        self.wfile.write(_END_OF_CHUNKS)
        self.wfile.flush()

    def _write_chunk(self, chunk: bytes) -> None:
        self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
        self.wfile.flush()

    def _reply(self, status: HTTPStatus, message: str) -> None:
        body = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
        self.wfile.flush()

    @override
    def log_message(self, format: str, *args: object) -> None:
        log.debug("vertex proxy %s", format % args)


class VertexAnthropicProxy:
    """Loopback-only adapter from the Anthropic Messages API to Vertex Anthropic ``rawPredict``."""

    def __init__(
        self,
        config: VertexAnthropicConfig,
        tokens: TokenCache | None = None,
        upstream_timeout: float = _UPSTREAM_TIMEOUT,
    ) -> None:
        """Serve requests for config, using tokens and failing upstream calls after upstream_timeout seconds."""
        self._config = config
        self._tokens = tokens if tokens is not None else TokenCache()
        self._timeout = upstream_timeout
        self._running: tuple[_VertexServer, threading.Thread] | None = None

    def __enter__(self) -> Self:
        """Start the proxy so a ``with`` block can hand its URL to the client."""
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        """Stop the proxy when the ``with`` block ends, however it ends."""
        self.stop()

    @property
    def url(self) -> str:
        """Base URL to hand to the Anthropic client; only available once the proxy runs."""
        if self._running is None:
            msg = "the Vertex Anthropic proxy is not running"
            raise VertexProxyError(msg)
        return f"http://{LOOPBACK_HOST}:{self._running[0].server_address[1]}"

    def start(self) -> Self:
        """Bind an ephemeral loopback port and serve requests in a background thread."""
        if self._running is not None:
            return self
        server = _VertexServer((LOOPBACK_HOST, 0), _VertexHandler)
        server.config = self._config
        server.tokens = self._tokens
        server.upstream_timeout = self._timeout
        thread = threading.Thread(target=server.serve_forever, name="vertex-anthropic-proxy", daemon=True)
        thread.start()
        self._running = (server, thread)
        log.info("vertex anthropic proxy for project %s listening on %s", self._config.project, self.url)
        return self

    def stop(self) -> None:
        """Stop serving and release the loopback port; a no-op when the proxy is not running."""
        if self._running is None:
            return
        server, thread = self._running
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        self._running = None
