"""Run: uvicorn creeper_dripper.dashboard.app:app --host 127.0.0.1 --port 8765"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8765"))
    uvicorn.run("creeper_dripper.dashboard.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
