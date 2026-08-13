"""Thread-safe control state for deterministic simulator behavior."""

from enum import StrEnum
from threading import Lock


class SimulatorMode(StrEnum):
    """The deterministic downstream behaviors available to local experiments."""

    HEALTHY = "HEALTHY"
    RETURN_500 = "RETURN_500"
    SLOW = "SLOW"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


SLOW_DELAY_SECONDS = 1.0
TIMEOUT_DELAY_SECONDS = 6.0

MODE_DELAY_SECONDS: dict[SimulatorMode, float] = {
    SimulatorMode.HEALTHY: 0.0,
    SimulatorMode.RETURN_500: 0.0,
    SimulatorMode.SLOW: SLOW_DELAY_SECONDS,
    SimulatorMode.TIMEOUT: TIMEOUT_DELAY_SECONDS,
    SimulatorMode.UNAVAILABLE: 0.0,
}


def simulator_delay_seconds(mode: SimulatorMode) -> float:
    """Return the fixed response delay for one simulator mode."""
    return MODE_DELAY_SECONDS[mode]


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
