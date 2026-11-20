# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the loopback plumbing the proxies share: body length, chunked relay and error reply."""

import io
from collections.abc import Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import pytest

from review_assistant import proxy

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

    import requests


class _RecordingHandler:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.sent: list[tuple[str, object]] = []
        self.close_connection = False

    def send_response(self, status: int) -> None:
        self.sent.append(("status", status))

    def send_header(self, name: str, value: str) -> None:
        self.sent.append((name, value))

    def end_headers(self) -> None:
        self.sent.append(("end", None))


class _FakeUpstream:
    def __init__(self, status_code: int = HTTPStatus.OK, chunks: Sequence[bytes] = ()) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": "text/event-stream"}
        self._chunks = chunks

    def iter_content(self, chunk_size: int) -> Sequence[bytes]:
        assert chunk_size == proxy.READ_CHUNK
        return self._chunks


def _handler(headers: dict[str, str] | None = None) -> _RecordingHandler:
    return _RecordingHandler(headers)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({"Content-Length": "12"}, 12, id="valid"),
        pytest.param({}, 0, id="absent"),
        pytest.param({"Content-Length": "twelve"}, 0, id="malformed"),
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


def test_reply_states_the_length_and_closes() -> None:
    fake = _handler()
    proxy.reply(cast("BaseHTTPRequestHandler", fake), HTTPStatus.BAD_GATEWAY, b"nope", "text/plain")
    assert fake.wfile.getvalue() == b"nope"
    assert ("Content-Length", "4") in fake.sent
    assert ("status", HTTPStatus.BAD_GATEWAY) in fake.sent
    assert fake.close_connection is True
