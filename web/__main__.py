"""`python -m web` -- serve the read-only dashboard locally.

    python -m web                                   # config.json's `web` section (default: newest snapshot)
    python -m web --source snapshot --snapshot data/ledger_backup_20260910T105451Z.csv
    python -m web --source db                       # the ledger file, re-read every 300 s
    python -m web --host 0.0.0.0 --port 8765        # e.g. to reach it over Tailscale

Flags win over config.json and the environment, the same way a command-line value should. Binds to
127.0.0.1 unless told otherwise: there is no authentication in phase 1, so anything beyond
localhost / Tailscale is on you.
"""

from __future__ import annotations

import argparse
import sys

from config.settings import settings
from web.ledger_reader import BACKENDS, reader_from_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m web", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=settings.web_bind_host,
                        help=f"bind address (default {settings.web_bind_host!r}; "
                             "config web.bind_host / WEB_BIND_HOST)")
    parser.add_argument("--port", type=int, default=settings.web_port,
                        help=f"port (default {settings.web_port}; config web.port / WEB_PORT)")
    parser.add_argument("--source", choices=BACKENDS, default=None,
                        help=f"ledger backend (default {settings.web_ledger_source!r}; "
                             "config web.ledger_source / WEB_LEDGER_SOURCE)")
    parser.add_argument("--snapshot", default=None,
                        help="a specific CSV for the snapshot backend (default: the newest "
                             "data/ledger_backup_*.csv; config web.snapshot_path / WEB_SNAPSHOT_PATH)")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    args = parser.parse_args(argv)

    import uvicorn

    from web.app import create_app

    reader = reader_from_settings(settings, source=args.source, snapshot_path=args.snapshot)
    if reader.backend == "snapshot":
        print(f"Serving the snapshot {reader.resolve()} on http://{args.host}:{args.port}/",
              file=sys.stderr)
    else:
        print(f"Serving the ledger ({reader.ttl_seconds:g}s cache) on "
              f"http://{args.host}:{args.port}/", file=sys.stderr)
    if args.reload:
        uvicorn.run("web.app:app", host=args.host, port=args.port, reload=True)
    else:
        uvicorn.run(create_app(reader, settings=settings), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
