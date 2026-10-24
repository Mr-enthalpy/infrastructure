"""Unix-socket client for the broker."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any

from .errors import BrokerUnavailable, ProtocolError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass
class BrokerClient:
    socket_path: str
    timeout: float = 10.0
    max_response_bytes: int = MAX_RESPONSE_BYTES

    @staticmethod
    def _family() -> int:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise BrokerUnavailable("Unix domain sockets are not supported on this platform")
        return int(family)

    def _decode_response(self, response: bytes) -> dict[str, Any]:
        if not response.endswith(b"\n"):
            raise ProtocolError("broker closed the connection without a complete response")
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ProtocolError("broker returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ProtocolError("broker response must be a JSON object")
        return decoded

    def _read_line(self, client: socket.socket) -> dict[str, Any]:
        response = b""
        while not response.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
            if len(response) > self.max_response_bytes:
                raise ProtocolError("broker response is too large")
        return self._decode_response(response)

    def _read_line_stream(self, client: socket.socket) -> dict[str, Any]:
        """Read exactly the JSON control line without consuming stream bytes.

        Streaming commands switch the same socket to a raw byte relay after the
        control response. A normal buffered ``recv`` can coalesce the JSON line
        with the first SSH/PTY bytes, so this path reads one byte at a time until
        the newline. The control line is tiny and this happens only once.
        """

        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = client.recv(1)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > self.max_response_bytes:
                raise ProtocolError("broker response is too large")
        return self._decode_response(bytes(response))

    def request(self, command_name: str, **args: Any) -> dict[str, Any]:
        payload = (
            json.dumps({"cmd": command_name, "args": args}, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        try:
            with socket.socket(self._family(), socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.socket_path)
                client.sendall(payload.encode("utf-8"))
                return self._read_line(client)
        except ProtocolError:
            raise
        except OSError as exc:
            raise BrokerUnavailable(f"cannot reach broker: {exc}") from exc

    def connect_file(
        self,
        allocation_id: str,
        path: str,
        *,
        upload: bool,
    ) -> tuple[socket.socket, dict[str, Any]]:
        command = "file_put" if upload else "file_get"
        payload = (
            json.dumps(
                {
                    "cmd": command,
                    "args": {"id": allocation_id, "path": path},
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        client = socket.socket(self._family(), socket.SOCK_STREAM)
        try:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(payload.encode("utf-8"))
            response = self._read_line_stream(client)
            client.settimeout(None)
        except OSError as exc:
            client.close()
            raise BrokerUnavailable(f"cannot reach broker: {exc}") from exc
        except Exception:
            client.close()
            raise
        return client, response

    def connect_shell(
        self,
        allocation_id: str,
        cols: int,
        rows: int,
        job: str | None = None,
    ) -> tuple[socket.socket, dict[str, Any]]:
        payload = (
            json.dumps(
                {
                    "cmd": "shell",
                    "args": {
                        "id": allocation_id,
                        "cols": cols,
                        "rows": rows,
                        **({"job": job} if job else {}),
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        client = socket.socket(self._family(), socket.SOCK_STREAM)
        try:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(payload.encode("utf-8"))
            response = self._read_line_stream(client)
            client.settimeout(None)
        except OSError as exc:
            client.close()
            raise BrokerUnavailable(f"cannot reach broker: {exc}") from exc
        except Exception:
            client.close()
            raise
        return client, response

    def connect_tunnel(self, allocation_id: str) -> tuple[socket.socket, dict[str, Any]]:
        payload = (
            json.dumps(
                {
                    "cmd": "tunnel",
                    "args": {"id": allocation_id},
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        client = socket.socket(self._family(), socket.SOCK_STREAM)
        try:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(payload.encode("utf-8"))
            response = self._read_line_stream(client)
            client.settimeout(None)
        except OSError as exc:
            client.close()
            raise BrokerUnavailable(f"cannot reach broker: {exc}") from exc
        except Exception:
            client.close()
            raise
        return client, response
