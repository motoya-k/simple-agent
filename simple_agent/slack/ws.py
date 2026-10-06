"""A WebSocket client in one file, because the alternative is a dependency.

Slack's Socket Mode is the only part of this repo that needs a protocol the
standard library does not speak.  The rest of RFC 6455 is not here: no
extensions, no compression, no binary messages, no server role.  What is here
is what a client needs to hold one long-lived connection open — the handshake,
masked text frames, and the ping/pong that keeps a NAT from quietly dropping
it.

The same trade as :mod:`simple_agent.providers.sigv4`: ~150 lines of a
well-specified protocol, read once, against a dependency tree that has to be
trusted and upgraded forever.

Everything here blocks; :class:`~simple_agent.slack.socket.SlackSource` calls
it from a worker thread.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import ssl
import struct
import threading
import urllib.parse

log = logging.getLogger(__name__)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455, section 1.3

TEXT, BINARY, CONTINUATION = 0x1, 0x2, 0x0
CLOSE, PING, PONG = 0x8, 0x9, 0xA

#: A frame bigger than this is refused rather than buffered.  Nothing Slack
#: sends comes close; a peer that claims a 4 GiB payload is a peer to drop.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class WebSocketError(OSError):
    """The connection is unusable. The caller reconnects; it never retries a frame."""


def connect(
    url: str,
    *,
    connect_timeout: float = 30.0,
    read_timeout: float = 60.0,
    create_connection=socket.create_connection,
    ssl_context: ssl.SSLContext | None = None,
) -> "WebSocket":
    """Open ``wss://…`` and complete the handshake, or raise.

    ``read_timeout`` is what makes a half-open connection recoverable: a
    socket that has gone quiet raises instead of blocking forever, and the
    caller reconnects.  Slack sends a ping about every 30 seconds, so silence
    well past that means the connection is gone whatever the kernel thinks.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "wss":
        raise ValueError(f"wss:// only, not {parts.scheme!r} (TLS is not optional here)")
    host = parts.hostname or ""
    port = parts.port or 443
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query

    key = base64.b64encode(os.urandom(16)).decode()
    raw = create_connection((host, port), timeout=connect_timeout)
    try:
        context = ssl_context or ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise

    request = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    connection = WebSocket(sock, read_timeout=read_timeout)
    connection._finish_handshake(key)
    return connection


class WebSocket:
    """One open connection. Not thread-safe for reads; sends are serialized.

    Reading from two threads would interleave frames of one message, so the
    source reads on one thread only.  ``send`` is called from another (an ack
    while a read is in flight) and takes a lock.
    """

    def __init__(self, sock, *, read_timeout: float = 60.0) -> None:
        self._sock = sock
        self._reader = sock.makefile("rb")
        self._send_lock = threading.Lock()
        self._closed = False
        sock.settimeout(read_timeout)

    # -- handshake ------------------------------------------------------
    def _finish_handshake(self, key: str) -> None:
        status = self._reader.readline(8192).decode("latin-1").strip()
        headers = {}
        while True:
            line = self._reader.readline(8192).decode("latin-1")
            if line in ("\r\n", "\n", ""):
                break
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if "101" not in status:
            self.close()
            raise WebSocketError(f"handshake refused: {status or 'no response'}")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            # Either not a WebSocket server or something in the middle is
            # answering for it. Both mean: do not send frames down this socket.
            self.close()
            raise WebSocketError("handshake accept key did not match")

    # -- messages -------------------------------------------------------
    def recv(self) -> str | None:
        """The next text message, or ``None`` once the peer has closed.

        Control frames are handled here rather than returned: a ping is
        answered, a pong ignored, a close answered and reported as the end.
        """
        payload = bytearray()
        while True:
            opcode, data, fin = self._read_frame()
            if opcode == CLOSE:
                self._send_frame(CLOSE, data[:2])  # echo the status code back
                self._closed = True
                return None
            if opcode == PING:
                self._send_frame(PONG, data)
                continue
            if opcode == PONG:
                continue
            if opcode == BINARY:
                raise WebSocketError("binary frame on a text-only connection")
            if opcode not in (TEXT, CONTINUATION):
                raise WebSocketError(f"unknown opcode {opcode:#x}")

            payload += data
            if len(payload) > MAX_MESSAGE_BYTES:
                raise WebSocketError(f"message over {MAX_MESSAGE_BYTES} bytes")
            if fin:
                return payload.decode("utf-8", "replace")

    def send(self, text: str) -> None:
        self._send_frame(TEXT, text.encode("utf-8"))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._send_frame(CLOSE, struct.pack("!H", 1000))
            except OSError:
                pass
        for handle in (self._reader, self._sock):
            try:
                handle.close()
            except OSError:
                pass

    # -- framing --------------------------------------------------------
    def _read_frame(self) -> tuple[int, bytes, bool]:
        header = self._exactly(2)
        first, second = header[0], header[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._exactly(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._exactly(8))
        if length > MAX_MESSAGE_BYTES:
            raise WebSocketError(f"frame claims {length} bytes")
        mask = self._exactly(4) if masked else b""
        data = self._exactly(length) if length else b""
        if masked:
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        return opcode, data, fin

    def _send_frame(self, opcode: int, data: bytes) -> None:
        # Every client frame is masked — a server that sees an unmasked one is
        # required to drop the connection.
        mask = os.urandom(4)
        length = len(data)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def _exactly(self, count: int) -> bytes:
        data = self._reader.read(count)
        if data is None or len(data) < count:
            raise WebSocketError("connection closed mid-frame")
        return data
