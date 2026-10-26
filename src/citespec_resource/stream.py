"""Length-prefixed frames for the interactive shell stream."""

from __future__ import annotations

import asyncio
import json
import socket
import struct

from .errors import ProtocolError

FRAME_DATA = b"D"
FRAME_RESIZE = b"R"
FRAME_EXIT = b"E"
FRAME_HEADER = struct.Struct(">cI")
MAX_FRAME_BYTES = 1024 * 1024


def encode_frame(kind: bytes, payload: bytes) -> bytes:
    if len(kind) != 1:
        raise ValueError("frame kind must be exactly one byte")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("frame payload is too large")
    return FRAME_HEADER.pack(kind, len(payload)) + payload


def decode_frames(buffer: bytes) -> tuple[list[tuple[bytes, bytes]], bytes]:
    frames: list[tuple[bytes, bytes]] = []
    offset = 0
    while len(buffer) - offset >= FRAME_HEADER.size:
        kind, length = FRAME_HEADER.unpack_from(buffer, offset)
        if length > MAX_FRAME_BYTES:
            raise ProtocolError("shell frame is too large")
        end = offset + FRAME_HEADER.size + length
        if end > len(buffer):
            break
        payload = buffer[offset + FRAME_HEADER.size : end]
        frames.append((kind, payload))
        offset = end
    return frames, buffer[offset:]


def send_frame(sock: socket.socket, kind: bytes, payload: bytes) -> None:
    sock.sendall(encode_frame(kind, payload))


async def read_frame(
    reader: asyncio.StreamReader,
) -> tuple[bytes, bytes] | None:
    try:
        header = await reader.readexactly(FRAME_HEADER.size)
    except asyncio.IncompleteReadError:
        return None
    kind, length = FRAME_HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError("shell frame is too large")
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        return None
    return kind, payload


async def write_frame(
    writer: asyncio.StreamWriter,
    kind: bytes,
    payload: bytes,
) -> None:
    writer.write(encode_frame(kind, payload))
    await writer.drain()


def resize_payload(cols: int, rows: int) -> bytes:
    if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
        raise ValueError("terminal size is out of range")
    return f"{cols}x{rows}".encode("ascii")


def parse_resize(payload: bytes) -> tuple[int, int]:
    try:
        text = payload.decode("ascii")
        cols_text, rows_text = text.split("x", 1)
        cols = int(cols_text)
        rows = int(rows_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("invalid resize frame") from exc
    if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
        raise ProtocolError("terminal size is out of range")
    return cols, rows


def exit_payload(reason: str = "", code: int = 0) -> bytes:
    return json.dumps({"code": code, "reason": reason}, sort_keys=True).encode("utf-8")
