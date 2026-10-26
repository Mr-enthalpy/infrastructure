"""Shared data types for allocations and GPU inventory."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

AllocationStatus = Literal["pending", "active", "released", "expired", "failed"]


@dataclass(frozen=True)
class GPU:
    index: int
    uuid: str
    name: str
    memory_mib: int
    used_mib: int = 0
    utilization: int = 0
    compute_pids: tuple[int, ...] = ()


@dataclass
class Allocation:
    id: str
    owner_uid: int
    owner_name: str
    gpus: tuple[str, ...]
    cpus: float
    memory_gb: int
    image: str
    workspace: str
    note: str
    starts_at: datetime
    expires_at: datetime
    status: AllocationStatus = "pending"
    container_id: str = ""
    container_name: str = ""
    events: list[dict] = field(default_factory=list)
