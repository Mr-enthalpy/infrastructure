import asyncio
import getpass
import json
import os
import socket

import pytest

from tests.fakes import FakeDocker, FakeIdentityResolver
from udc_resource.broker.server import Broker
from udc_resource.client import BrokerClient
from udc_resource.identity import Identity
from udc_resource.stream import FRAME_DATA, FRAME_EXIT, read_frame, write_frame

pytestmark = [
    pytest.mark.linux,
    pytest.mark.skipif(
        os.name != "posix" or not hasattr(socket, "SO_PEERCRED"),
        reason="SO_PEERCRED and Unix sockets are required",
    ),
]


async def test_uds_peer_identity_and_client(tmp_path, active_config):
    uid = os.getuid()
    gid = os.getgid()
    identity = Identity(
        uid=uid,
        gid=gid,
        username=getpass.getuser(),
        role="member",
        is_admin=False,
    )
    socket_path = tmp_path / "broker.sock"
    broker = Broker(
        active_config,
        db_path=tmp_path / "state.db",
        socket_path=socket_path,
        docker=FakeDocker(),
        identity_resolver=FakeIdentityResolver({uid: identity}),
    )
    await broker.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(b"not-json\n")
        await writer.drain()
        invalid = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert invalid["ok"] is False
        assert invalid["error"]["code"] == "bad_request"

        writer.write(b'{"cmd":"ping","args":{}}\n')
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert response["ok"] is True
        assert response["data"]["uid"] == uid
        assert response["data"]["username"] == identity.username

        client = BrokerClient(str(socket_path), timeout=5)
        client_response = await asyncio.to_thread(client.request, "ping")
        assert client_response["ok"] is True
        assert client_response["data"]["uid"] == uid

        writer.close()
        await writer.wait_closed()
    finally:
        await broker.stop()


async def test_uds_shell_stream(tmp_path, active_config):
    uid = os.getuid()
    gid = os.getgid()
    identity = Identity(
        uid=uid,
        gid=gid,
        username=getpass.getuser(),
        role="member",
        is_admin=False,
    )
    socket_path = tmp_path / "broker.sock"
    fake = FakeDocker()
    fake.shell_output = b"shell ready\r\n"
    broker = Broker(
        active_config,
        db_path=tmp_path / "state.db",
        socket_path=socket_path,
        docker=fake,
        identity_resolver=FakeIdentityResolver({uid: identity}),
    )
    await broker.start()
    try:
        applied = await broker.handle_request(
            {
                "cmd": "apply",
                "args": {"gpus": [0], "cpus": 2, "memory_gb": 4, "hours": 1},
            },
            uid=uid,
            gid=gid,
        )
        assert applied["ok"] is True, applied
        allocation_id = applied["data"]["id"]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        request = {
            "cmd": "shell",
            "args": {"id": allocation_id, "cols": 100, "rows": 30},
        }
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        handshake = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert handshake["ok"] is True
        kind, payload = await asyncio.wait_for(read_frame(reader), timeout=5)
        assert kind == FRAME_DATA
        assert payload == b"shell ready\r\n"
        kind, _payload = await asyncio.wait_for(read_frame(reader), timeout=5)
        assert kind == FRAME_EXIT
        assert fake.shells
        assert fake.shells[0].sizes[0] == (100, 30)
        await asyncio.sleep(0.05)
        assert fake.shells[0].closed is True
        writer.close()
        await writer.wait_closed()
    finally:
        await broker.stop()


async def test_uds_file_transfer_stays_in_workspace(tmp_path, active_config):
    uid = os.getuid()
    gid = os.getgid()
    identity = Identity(
        uid=uid,
        gid=gid,
        username=getpass.getuser(),
        role="member",
        is_admin=False,
    )
    socket_path = tmp_path / "broker.sock"
    broker = Broker(
        active_config,
        db_path=tmp_path / "state.db",
        socket_path=socket_path,
        docker=FakeDocker(),
        identity_resolver=FakeIdentityResolver({uid: identity}),
    )
    await broker.start()
    try:
        applied = await broker.handle_request(
            {
                "cmd": "apply",
                "args": {"gpus": [0], "cpus": 2, "memory_gb": 4, "hours": 1},
            },
            uid=uid,
            gid=gid,
        )
        assert applied["ok"] is True, applied
        allocation_id = applied["data"]["id"]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(
            json.dumps(
                {
                    "cmd": "file_put",
                    "args": {"id": allocation_id, "path": "checkpoints/model.bin"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        handshake = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert handshake["ok"] is True
        await write_frame(writer, FRAME_DATA, b"hello ")
        await write_frame(writer, FRAME_DATA, b"world")
        await write_frame(writer, FRAME_EXIT, b"{}")
        kind, payload = await asyncio.wait_for(read_frame(reader), timeout=5)
        assert kind == FRAME_EXIT
        result = json.loads(payload)
        assert result["ok"] is True
        assert result["data"]["bytes"] == 11
        uploaded = (
            active_config.server.workspace_root / identity.username / "checkpoints" / "model.bin"
        )
        assert uploaded.read_bytes() == b"hello world"
        writer.close()
        await writer.wait_closed()

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(
            json.dumps(
                {
                    "cmd": "file_get",
                    "args": {"id": allocation_id, "path": "checkpoints/model.bin"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        handshake = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert handshake["ok"] is True
        kind, payload = await asyncio.wait_for(read_frame(reader), timeout=5)
        assert kind == FRAME_DATA
        assert payload == b"hello world"
        kind, _payload = await asyncio.wait_for(read_frame(reader), timeout=5)
        assert kind == FRAME_EXIT
        writer.close()
        await writer.wait_closed()

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(
            json.dumps(
                {
                    "cmd": "file_put",
                    "args": {"id": allocation_id, "path": "../escape"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        handshake = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert handshake["ok"] is False
        assert handshake["error"]["code"] == "forbidden"

        writer.close()
        await writer.wait_closed()
    finally:
        await broker.stop()
