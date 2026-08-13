"""Thread-safe control state for deterministic simulator behavior."""

from enum import StrEnum
from threading import Lock


class SimulatorMode(StrEnum):
    """The downstream behaviors available in the first control step."""

    HEALTHY = "HEALTHY"
    RETURN_500 = "RETURN_500"


class SimulatorControl:
    """Store the active simulator mode safely within one process."""

    def __init__(self) -> None:
        self._mode = SimulatorMode.HEALTHY
        self._lock = Lock()

    def get_mode(self) -> SimulatorMode:
        """Return the currently active mode."""
        with self._lock:
            return self._mode

    def set_mode(self, mode: SimulatorMode) -> SimulatorMode:
        """Activate and return one validated mode."""
        with self._lock:
            self._mode = mode
            return self._mode

    def reset(self) -> None:
        """Restore the deterministic default mode."""
        self.set_mode(SimulatorMode.HEALTHY)
