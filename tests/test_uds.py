import asyncio
import getpass
import json
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fakes import FakeDocker, FakeIdentityResolver
from udc_resource.broker.server import Broker
from udc_resource.client import BrokerClient
from udc_resource.gpu_audit import GpuAuditResult
from udc_resource.identity import Identity
from udc_resource.legacy_audit import LegacyAuditResult
from udc_resource.stream import FRAME_DATA, FRAME_EXIT, read_frame, write_frame
from udc_resource.workspace_quota import MemberQuotaResult, QuotaCheckResult


def _passing_member_quota(workspace: str, uid: int, limit: int) -> MemberQuotaResult:
    return MemberQuotaResult(True, "ok", project_id=uid, hard_limit_bytes=limit * 1024**3)


def _uds_broker_kwargs() -> dict:
    """Active mode enforces the mandatory gates at startup; give the broker
    passing audit and quota runners so the UDS tests focus on transport."""
    return {
        "legacy_audit": lambda: LegacyAuditResult(True, "clean"),
        "gpu_audit": lambda: GpuAuditResult(True, "clean", fingerprint="fp"),
        "quota_check": lambda: QuotaCheckResult(True, "ok"),
        "member_quota_check": _passing_member_quota,
        "member_quota_provision": _passing_member_quota,
    }


pytestmark = [
    pytest.mark.linux,
    pytest.mark.skipif(
        os.name != "posix" or not hasattr(socket, "SO_PEERCRED"),
        reason="SO_PEERCRED and Unix sockets are required",
    ),
]


def _uds_broker(tmp_path, config, socket_path, docker, identity_resolver):
    """Active mode enforces the mandatory gates at startup; give the broker
    passing audit and quota runners so the UDS tests focus on transport."""
    from udc_resource.gpu_audit import GpuAuditResult
    from udc_resource.legacy_audit import LegacyAuditResult
    from udc_resource.workspace_quota import MemberQuotaResult, QuotaCheckResult

    def passing_member_quota(workspace, uid, limit):
        return MemberQuotaResult(True, "ok", project_id=uid, hard_limit_bytes=limit * 1024**3)

    return Broker(
        config,
        db_path=tmp_path / "state.db",
        socket_path=socket_path,
        docker=docker,
        identity_resolver=identity_resolver,
        legacy_audit=lambda: LegacyAuditResult(True, "clean"),
        gpu_audit=lambda: GpuAuditResult(True, "clean", fingerprint="fp"),
        quota_check=lambda: QuotaCheckResult(True, "ok"),
        member_quota_check=passing_member_quota,
        member_quota_provision=passing_member_quota,
    )


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
        **_uds_broker_kwargs(),
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
        **_uds_broker_kwargs(),
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
        **_uds_broker_kwargs(),
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
        workspace = Path(str(applied["data"]["workspace"]))
        uploaded = workspace / "checkpoints" / "model.bin"
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


async def test_uds_upload_limit_aborts_without_partial_file(tmp_path, active_config):
    uid = os.getuid()
    gid = os.getgid()
    identity = Identity(
        uid=uid,
        gid=gid,
        username=getpass.getuser(),
        role="member",
        is_admin=False,
    )
    config = replace(
        active_config,
        policy=replace(active_config.policy, max_upload_bytes=5),
    )
    socket_path = tmp_path / "broker.sock"
    broker = Broker(
        config,
        db_path=tmp_path / "state.db",
        socket_path=socket_path,
        docker=FakeDocker(),
        identity_resolver=FakeIdentityResolver({uid: identity}),
        **_uds_broker_kwargs(),
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
        allocation_id = applied["data"]["id"]
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(
            json.dumps(
                {
                    "cmd": "file_put",
                    "args": {"id": allocation_id, "path": "too-large.bin"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        handshake = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert handshake["ok"] is True
        await write_frame(writer, FRAME_DATA, b"123456")
        await write_frame(writer, FRAME_EXIT, b"{}")
        kind, payload = await asyncio.wait_for(read_frame(reader), timeout=5)
        assert kind == FRAME_EXIT
        result = json.loads(payload)
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_request"
        assert not (Path(applied["data"]["workspace"]) / "too-large.bin").exists()
        writer.close()
        await writer.wait_closed()
    finally:
        await broker.stop()
