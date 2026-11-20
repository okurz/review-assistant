# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the loopback plumbing the proxies share: body length, chunked relay and error reply."""

import io
import logging
from collections.abc import Buffer, Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING, cast, override

import pytest
import requests

from review_assistant import proxy

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler


class _RecordingHandler:
    def __init__(self, headers: dict[str, str] | None = None, wfile: io.BytesIO | None = None) -> None:
        self.headers = headers or {}
        self.wfile = wfile if wfile is not None else io.BytesIO()
        self.sent: list[tuple[str, object]] = []
        self.close_connection = False

    def send_response(self, status: int) -> None:
        self.sent.append(("status", status))

    def send_header(self, name: str, value: str) -> None:
        self.sent.append((name, value))

    def end_headers(self) -> None:
        self.sent.append(("end", None))


class _FakeUpstream:
    def __init__(self, status_code: int = HTTPStatus.OK, chunks: Sequence[bytes] = (), *, breaks: bool = False) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": "text/event-stream"}
        self._chunks = chunks
        self._breaks = breaks

    def iter_content(self, chunk_size: int) -> Sequence[bytes]:
        assert chunk_size == proxy.READ_CHUNK
        if self._breaks:
            raise requests.RequestException
        return self._chunks


class _ClosedClient(io.BytesIO):
    @override
    def write(self, _buffer: Buffer) -> int:
        raise BrokenPipeError


def _handler(headers: dict[str, str] | None = None, wfile: io.BytesIO | None = None) -> _RecordingHandler:
    return _RecordingHandler(headers, wfile)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({"Content-Length": "12"}, 12, id="valid"),
        pytest.param({}, 0, id="absent"),
        pytest.param({"Content-Length": "twelve"}, 0, id="malformed"),
        pytest.param({"Content-Length": "-4"}, 0, id="negative"),
    ],
)
def test_body_length_treats_a_bad_header_as_empty(headers: dict[str, str], expected: int) -> None:
    fake = _handler(headers)
    assert proxy.content_length(cast("BaseHTTPRequestHandler", fake)) == expected


def test_relay_writes_chunked_framing_and_skips_empty_chunks() -> None:
    fake = _handler()
    upstream = _FakeUpstream(chunks=(b"ab", b"", b"c"))
    proxy.relay(cast("BaseHTTPRequestHandler", fake), cast("requests.Response", upstream))
    assert fake.wfile.getvalue() == b"2\r\nab\r\n1\r\nc\r\n0\r\n\r\n"
    assert ("Transfer-Encoding", "chunked") in fake.sent
    assert fake.close_connection is True


@pytest.mark.parametrize(
    ("wfile", "upstream"),
    [
        pytest.param(None, _FakeUpstream(breaks=True), id="upstream-breaks"),
        pytest.param(_ClosedClient(), _FakeUpstream(chunks=(b"ab",)), id="client-hangs-up"),
    ],
)
def test_relay_stops_quietly_on_a_broken_stream(
    wfile: io.BytesIO | None, upstream: object, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _handler(wfile=wfile)
    with caplog.at_level(logging.DEBUG, logger="review_assistant.proxy"):
        proxy.relay(cast("BaseHTTPRequestHandler", fake), cast("requests.Response", upstream))
    assert "relay stopped early" in caplog.text


def test_reply_states_the_length_and_closes() -> None:
    fake = _handler()
    proxy.reply(cast("BaseHTTPRequestHandler", fake), HTTPStatus.BAD_GATEWAY, b"nope", "text/plain")
    assert fake.wfile.getvalue() == b"nope"
    assert ("Content-Length", "4") in fake.sent
    assert ("status", HTTPStatus.BAD_GATEWAY) in fake.sent
    assert fake.close_connection is True
