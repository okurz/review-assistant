# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Loopback passthrough that holds the inference key, so the review agent only ever sees a sentinel.

Hermes' ``custom`` provider speaks OpenAI chat completions and Helmcode serves the same wire, so the proxy forwards
each request unchanged and replaces only the ``Authorization`` header. The real key therefore stays in this process
and out of the agent's environment, its argv, every configuration file and the logs.
"""

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Self, cast, override
from urllib.parse import urlsplit

import requests

from review_assistant import proxy
from review_assistant.config import ConfigError

SENTINEL_API_KEY = "helmcode-proxy"
BASE_URL_ENV = "CUSTOM_BASE_URL"
API_KEY_ENV = "CUSTOM_API_KEY"
API_PATH = "/v1"
_UPSTREAM_TIMEOUT = 900.0
_STRIPPED_HEADERS = frozenset({"authorization", "connection", "content-length", "host"})
_CONTENT_TYPE = "application/json"

if TYPE_CHECKING:
    import threading

log = logging.getLogger(__name__)


class InferenceProxyError(ConfigError):
    """The inference proxy is misconfigured or not running."""


def inference_env(proxy_url: str | None) -> dict[str, str]:
    """Return the Hermes variables that point the custom provider at the proxy; empty when no proxy runs."""
    return {} if proxy_url is None else {BASE_URL_ENV: proxy_url, API_KEY_ENV: SENTINEL_API_KEY}


def _error_body(message: str) -> bytes:
    return json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode()


class _InferenceServer(proxy.LoopbackServer):
    """Loopback-only server carrying the upstream endpoint and the key it adds to every forwarded request."""

    upstream: str
    api_key: str
    upstream_timeout: float


class _InferenceHandler(BaseHTTPRequestHandler):
    """Forward ``/v1/…`` calls to the upstream endpoint, holding the credential the client never sees."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._forward("GET")

    def do_POST(self) -> None:
        self._forward("POST")

    def _forward(self, method: str) -> None:
        server = cast("_InferenceServer", self.server)
        path = urlsplit(self.path).path
        if not path.startswith(f"{API_PATH}/"):
            proxy.reply(self, HTTPStatus.NOT_FOUND, _error_body(f"only {API_PATH}/... is proxied"), _CONTENT_TYPE)
            return
        body = self.rfile.read(proxy.content_length(self)) if method == "POST" else None
        try:
            upstream = requests.request(
                method,
                f"{server.upstream}{self.path.removeprefix(API_PATH)}",
                data=body,
                headers=self._upstream_headers(server.api_key),
                stream=True,
                timeout=server.upstream_timeout,
            )
        except requests.RequestException as error:
            log.warning("inference request %s %s failed: %s", method, path, error.__class__.__name__)
            proxy.reply(self, HTTPStatus.BAD_GATEWAY, _error_body("inference upstream request failed"), _CONTENT_TYPE)
            return
        with upstream:
            proxy.relay(self, upstream)

    def _upstream_headers(self, api_key: str) -> dict[str, str]:
        forwarded = {name: value for name, value in self.headers.items() if name.lower() not in _STRIPPED_HEADERS}
        return {**forwarded, "Authorization": f"Bearer {api_key}"}

    @override
    def log_message(self, format: str, *args: object) -> None:
        log.debug("inference proxy %s", format % args)


class InferenceProxy:
    """Loopback-only passthrough to an OpenAI-compatible endpoint that needs a bearer key."""

    def __init__(self, api_base: str, api_key: str, upstream_timeout: float = _UPSTREAM_TIMEOUT) -> None:
        """Serve requests for api_base, adding api_key, and failing upstream calls after upstream_timeout seconds."""
        if not api_key.strip():
            msg = "the inference API key is missing; it is read from the environment, never from a configuration file"
            raise InferenceProxyError(msg)
        self._upstream = api_base.rstrip("/")
        self._api_key = api_key.strip()
        self._timeout = upstream_timeout
        self._running: tuple[_InferenceServer, threading.Thread] | None = None

    def __enter__(self) -> Self:
        """Start the proxy so a ``with`` block can hand its URL to the client."""
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        """Stop the proxy when the ``with`` block ends, however it ends."""
        self.stop()

    @property
    def url(self) -> str:
        """Base URL to hand to Hermes as ``CUSTOM_BASE_URL``; only available once the proxy runs."""
        if self._running is None:
            msg = "the inference proxy is not running"
            raise InferenceProxyError(msg)
        return f"http://{proxy.LOOPBACK_HOST}:{self._running[0].server_address[1]}{API_PATH}"

    def start(self) -> Self:
        """Bind an ephemeral loopback port and serve requests in a background thread."""
        if self._running is not None:
            return self
        server = _InferenceServer((proxy.LOOPBACK_HOST, 0), _InferenceHandler)
        server.upstream = self._upstream
        server.api_key = self._api_key
        server.upstream_timeout = self._timeout
        self._running = (server, proxy.start(server, "inference-proxy"))
        log.info("inference proxy for %s listening on %s", self._upstream, self.url)
        return self

    def stop(self) -> None:
        """Stop serving and release the loopback port; a no-op when the proxy is not running."""
        if self._running is None:
            return
        proxy.stop(*self._running)
        self._running = None
