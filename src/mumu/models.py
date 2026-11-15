"""Public values returned by the MuMu adapter."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class MuMuError(RuntimeError):
    """MuMu/ADB unavailable, timed out, or returned an invalid result."""


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    state: str
    adb_port: Optional[int] = None
    pid: Optional[int] = None


@dataclass(frozen=True)
class Screenshot:
    path: Path
    width: int
    height: int
    instance_id: int
    captured_at: float


@dataclass(frozen=True)
class StepResult:
    index: int
    action: str
    success: bool
    value: Any = None
    error: Optional[str] = None
