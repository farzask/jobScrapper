"""Start the JobApplier dashboard.

    python run.py            -> http://localhost:8000
    python run.py --no-open  -> don't launch a browser
"""
from __future__ import annotations

import logging
import sys
import threading
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", 8000


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if "--no-open" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    print(f"\n  JobApplier -> http://localhost:{PORT}\n  Ctrl+C to stop\n")
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
