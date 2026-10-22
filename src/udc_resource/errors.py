"""Typed errors shared by the broker, client, and CLI."""

from __future__ import annotations

from typing import Final

EXIT_CODES: Final[dict[str, int]] = {
    "bad_request": 2,
    "invalid_argument": 2,
    "gpu_busy": 3,
    "insufficient_resources": 3,
    "forbidden": 4,
    "monitor_only": 4,
    "not_found": 5,
    "expired": 5,
    "conflict": 5,
    "broker_unavailable": 6,
    "docker_error": 7,
    "protocol_error": 7,
    "internal": 7,
}


def exit_code_for(code: str) -> int:
    return EXIT_CODES.get(code, 7)


class UdcError(Exception):
    """Base class for errors that can cross the broker/client boundary."""

    code: str = "internal"
    exit_code: int = 7

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadRequest(UdcError):
    code = "bad_request"
    exit_code = 2


class InvalidArgument(UdcError):
    code = "invalid_argument"
    exit_code = 2


class GpuBusy(UdcError):
    code = "gpu_busy"
    exit_code = 3


class InsufficientResources(UdcError):
    code = "insufficient_resources"
    exit_code = 3


class Forbidden(UdcError):
    code = "forbidden"
    exit_code = 4


class MonitorOnly(UdcError):
    code = "monitor_only"
    exit_code = 4


class NotFound(UdcError):
    code = "not_found"
    exit_code = 5


class Expired(UdcError):
    code = "expired"
    exit_code = 5


class Conflict(UdcError):
    code = "conflict"
    exit_code = 5


class BrokerUnavailable(UdcError):
    code = "broker_unavailable"
    exit_code = 6


class ProtocolError(UdcError):
    code = "protocol_error"
    exit_code = 7


class DockerError(UdcError):
    code = "docker_error"
    exit_code = 7


class InternalError(UdcError):
    code = "internal"
    exit_code = 7
