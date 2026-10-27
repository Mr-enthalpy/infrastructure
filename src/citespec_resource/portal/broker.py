"""Narrow broker bridge used by the portal service account.

The portal process belongs to ``citespec-portal`` and ``citespec-cli`` but never
``citespec-admins``.  The broker recognizes it through ``is_portal`` and accepts
only the commands listed below.  The portal performs its own RBAC; the broker
re-validates that the asserted portal actor is an admin before any mutation and
records the actor in the broker event log.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..client import BrokerClient
from ..errors import BrokerUnavailable, ProtocolError

PORTAL_COMMANDS = frozenset(
    {
        "portal_status",
        "portal_member_self",
        "portal_registration_submit",
        "portal_requests_list",
        "portal_requests_show",
        "portal_requests_approve",
        "portal_requests_reject",
        "portal_members_list",
        "portal_members_show",
        "portal_members_suspend",
        "portal_members_resume",
        "portal_members_revoke",
        "portal_audit",
    }
)


class PortalBrokerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PortalActor:
    user_id: str
    username: str
    role: str
    member_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "member_id": self.member_id,
        }


class PortalBroker:
    def __init__(self, socket_path: str, *, timeout: float = 15.0) -> None:
        self.socket_path = socket_path
        self.client = BrokerClient(socket_path, timeout=timeout)

    async def call(
        self,
        command: str,
        *,
        actor: PortalActor,
        **args: Any,
    ) -> Any:
        if command not in PORTAL_COMMANDS:
            raise PortalBrokerError("forbidden", "portal command is not allowlisted")
        payload = {"actor": actor.as_dict(), **args}
        try:
            response = await asyncio.to_thread(self.client.request, command, **payload)
        except BrokerUnavailable as exc:
            raise PortalBrokerError("broker_unavailable", str(exc)) from exc
        except ProtocolError as exc:
            raise PortalBrokerError("protocol_error", str(exc)) from exc
        if not isinstance(response, dict):
            raise PortalBrokerError("protocol_error", "invalid broker response")
        if not response.get("ok"):
            error = response.get("error")
            if isinstance(error, dict):
                raise PortalBrokerError(
                    str(error.get("code", "internal")),
                    str(error.get("message", "broker request failed")),
                )
            raise PortalBrokerError("internal", "broker request failed")
        data = response.get("data")
        return {} if data is None else data
