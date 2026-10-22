import pytest

from udc_resource.errors import ProtocolError
from udc_resource.stream import (
    FRAME_DATA,
    FRAME_EXIT,
    FRAME_RESIZE,
    decode_frames,
    encode_frame,
    exit_payload,
    parse_resize,
    resize_payload,
)


def test_frames_roundtrip_with_partial_buffer():
    first = encode_frame(FRAME_DATA, b"hello")
    second = encode_frame(FRAME_RESIZE, resize_payload(120, 40))
    frames, rest = decode_frames(first + second[:3])
    assert frames == [(FRAME_DATA, b"hello")]
    assert rest == second[:3]
    frames, rest = decode_frames(rest + second[3:])
    assert frames == [(FRAME_RESIZE, resize_payload(120, 40))]
    assert rest == b""


def test_resize_and_exit_payloads():
    assert parse_resize(resize_payload(100, 30)) == (100, 30)
    assert b"shell" in exit_payload("shell exited")
    with pytest.raises(ProtocolError):
        parse_resize(b"not-a-size")


def test_oversized_frame_is_rejected():
    header = FRAME_EXIT + (2 * 1024 * 1024).to_bytes(4, "big")
    with pytest.raises(ProtocolError):
        decode_frames(header)
