# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Loopback HTTP plumbing shared by the credential-holding proxies: bind, relay chunks, answer, stop.

Each proxy keeps its own routing, header and body policy; only the socket and wire mechanics live here, so both
proxies relay a streamed upstream answer unbuffered and release their port the same way.
"""

import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

LOOPBACK_HOST = "127.0.0.1"
READ_CHUNK = 8192
_END_OF_CHUNKS = b"0\r\n\r\n"

log = logging.getLogger(__name__)


class LoopbackServer(ThreadingHTTPServer):
    """HTTP server that binds only to the loopback address of its caller."""


def start(server: ThreadingHTTPServer, name: str) -> threading.Thread:
    """Serve server in a daemon thread and return that thread, so the caller can stop it later."""
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    return thread


def stop(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    """Stop serving, release the port and wait for the serving thread to finish."""
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def content_length(handler: BaseHTTPRequestHandler) -> int:
    """Return the request body length, treating a missing, malformed or negative header as an empty body."""
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return 0
    return max(length, 0)


def relay(handler: BaseHTTPRequestHandler, upstream: requests.Response) -> None:
    """Stream upstream to the client as chunked HTTP, so a streamed answer reaches the agent unbuffered."""
    handler.send_response(upstream.status_code)
    handler.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    try:
        for chunk in filter(None, upstream.iter_content(chunk_size=READ_CHUNK)):
            handler.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            handler.wfile.flush()
        handler.wfile.write(_END_OF_CHUNKS)
        handler.wfile.flush()
    except (OSError, requests.RequestException) as error:
        # A client that hangs up or an upstream that breaks mid-stream must not surface as a traceback on stderr.
        log.debug("relay stopped early: %s", error.__class__.__name__)


def reply(handler: BaseHTTPRequestHandler, status: HTTPStatus, body: bytes, content_type: str) -> None:
    """Answer with an explicit length and no keep-alive, so the client reads the error and stops."""
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    handler.wfile.write(body)
    handler.wfile.flush()
