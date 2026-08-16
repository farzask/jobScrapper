"""Start the JobApplier dashboard.

    python run.py             -> http://localhost:8000
    python run.py --no-open   -> don't launch a browser
    python run.py --port 8123 -> pin a specific port
"""
from __future__ import annotations

import argparse
import logging
import socket
import threading
import webbrowser
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PORT_SEARCH_RANGE = 20


def port_is_free(port: int) -> bool:
    """True if the port can actually be bound.

    Note the deliberate absence of SO_REUSEADDR: on Windows that option lets a
    bind succeed against a port already in use, so setting it makes this check
    report busy ports as free. A connect-based probe isn't a good substitute
    either -- it misreads a listener whose accept backlog is full.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def jobapplier_already_on(port: int) -> bool:
    """True if the thing holding this port is another JobApplier instance."""
    try:
        with urlopen(f"http://{HOST}:{port}/api/state", timeout=2) as r:
            return r.status == 200 and b"running" in r.read(200)
    except (URLError, OSError, ValueError):
        return False


def pick_port(preferred: int, pinned: bool) -> int | None:
    """Resolve a usable port, or None if we should just stop.

    Binding failures here are routine -- an instance from earlier is usually
    still up -- so this resolves the common cases instead of dumping a
    WinError 10048 traceback on the user.
    """
    if port_is_free(preferred):
        return preferred

    if jobapplier_already_on(preferred):
        url = f"http://localhost:{preferred}"
        print(f"\n  JobApplier is already running at {url}")
        print("  Opening that instead of starting a second copy.")
        print(f"  To restart it: stop the other window, or run on another port"
              f" with --port {preferred + 1}\n")
        webbrowser.open(url)
        return None

    if pinned:
        print(f"\n  Port {preferred} is in use by another program.")
        print(f"  Pick a different one, e.g.  python run.py --port {preferred + 1}\n")
        return None

    for candidate in range(preferred + 1, preferred + PORT_SEARCH_RANGE):
        if port_is_free(candidate):
            print(f"\n  Port {preferred} is busy -- using {candidate} instead.")
            return candidate

    print(f"\n  No free port found between {preferred} and "
          f"{preferred + PORT_SEARCH_RANGE}.\n")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="JobApplier dashboard")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    port = pick_port(args.port or DEFAULT_PORT, pinned=args.port is not None)
    if port is None:
        return

    if not args.no_open:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    print(f"\n  JobApplier -> http://localhost:{port}\n  Ctrl+C to stop\n")
    uvicorn.run("app.main:app", host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
