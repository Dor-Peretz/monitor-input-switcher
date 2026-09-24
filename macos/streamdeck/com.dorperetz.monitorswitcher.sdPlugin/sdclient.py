"""Minimal Stream Deck WebSocket client (RFC 6455 subset, stdlib only).

Stream Deck launches a plugin as its own process and expects it to connect
back to a local WebSocket server. The official SDK is JavaScript-only, so
rather than pull in a dependency this implements the small slice of RFC 6455
the Stream Deck server actually speaks: text frames, ping/pong, and close.

Server-to-client frames are never masked; client-to-server frames always must
be, per the spec.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
from typing import Callable

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class StreamDeckClient:
    def __init__(self, port: int, plugin_uuid: str, register_event: str) -> None:
        self._port = port
        self._uuid = plugin_uuid
        self._register_event = register_event
        self._sock: socket.socket | None = None
        self._reader = None
        self._send_lock = threading.Lock()
        self._closed = False

    # -- connection ------------------------------------------------------- #

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self._port), timeout=10)
        sock.settimeout(None)
        self._sock = sock
        self._reader = sock.makefile("rb")
        self._handshake()
        self.send(self._register_event, uuid=self._uuid)

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        assert self._sock is not None
        self._sock.sendall(request.encode("ascii"))

        status_line = self._read_header_line()
        if "101" not in status_line:
            raise ConnectionError(f"WebSocket upgrade refused: {status_line.strip()}")
        while True:
            line = self._read_header_line()
            if line in ("\r\n", "\n", ""):
                break

    def _read_header_line(self) -> str:
        assert self._reader is not None
        return self._reader.readline().decode("latin-1")

    # -- framing ---------------------------------------------------------- #

    def _read_exact(self, count: int) -> bytes:
        assert self._reader is not None
        data = self._reader.read(count)
        if data is None or len(data) < count:
            raise ConnectionError("Stream Deck closed the connection.")
        return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        header = self._read_exact(2)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]

        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask is not None:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def _write_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed or self._sock is None:
            return
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))

        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        with self._send_lock:
            if self._closed or self._sock is None:
                return
            self._sock.sendall(bytes(header) + masked)

    # -- protocol --------------------------------------------------------- #

    def send(self, event: str, **fields) -> None:
        message = {"event": event}
        message.update({k: v for k, v in fields.items() if v is not None})
        self._write_frame(OPCODE_TEXT, json.dumps(message).encode("utf-8"))

    def run(self, on_message: Callable[[dict], None]) -> None:
        """Read messages until the socket closes. Blocks the calling thread."""
        pending = bytearray()
        pending_opcode = OPCODE_TEXT

        while not self._closed:
            try:
                fin, opcode, payload = self._read_frame()
            except (ConnectionError, OSError, struct.error):
                return

            if opcode == OPCODE_CLOSE:
                self._write_frame(OPCODE_CLOSE, payload[:2])
                return
            if opcode == OPCODE_PING:
                self._write_frame(OPCODE_PONG, payload)
                continue
            if opcode == OPCODE_PONG:
                continue

            if opcode == OPCODE_CONTINUATION:
                pending.extend(payload)
            else:
                pending = bytearray(payload)
                pending_opcode = opcode
            if not fin:
                continue

            if pending_opcode == OPCODE_TEXT:
                try:
                    message = json.loads(bytes(pending).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict):
                    on_message(message)
            pending = bytearray()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._write_frame(OPCODE_CLOSE, struct.pack(">H", 1000))
        except OSError:
            pass
        self._closed = True
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
        self._sock = None
