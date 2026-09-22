from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("BOTTAZZI_APP_HOST", "127.0.0.1").strip()
    port = int(os.getenv("BOTTAZZI_APP_PORT", "19091"))
    if not host:
        raise SystemExit("BOTTAZZI_APP_HOST must not be empty")
    if port < 1024 or port > 65535:
        raise SystemExit("BOTTAZZI_APP_PORT must be between 1024 and 65535")
    uvicorn.run(
        "openshell_backend.app_gateway:app",
        host=host,
        port=port,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=host + ",127.0.0.1",
    )


if __name__ == "__main__":
    main()
