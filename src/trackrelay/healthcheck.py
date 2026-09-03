"""Small stdlib-only liveness probe, independent of application imports."""

import sys
from http.client import HTTPConnection, HTTPException


def check_liveness(port: int) -> bool:
    """Require loopback HTTP 200 with a two-second socket timeout."""
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/health/live")
        return connection.getresponse().status == 200
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def main() -> int:
    """Allow only the two HTTP container roles."""
    if len(sys.argv) != 2 or sys.argv[1] not in {"8000", "8001"}:
        return 1
    return 0 if check_liveness(int(sys.argv[1])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
