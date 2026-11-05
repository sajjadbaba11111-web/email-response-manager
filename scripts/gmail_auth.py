"""One-time OAuth flow for Gmail.

Run this script once (from the project root) to authorise the application
against *your own* Google account and write the refresh token to the path
configured by ``GMAIL_TOKEN_PATH`` (``token.json`` by default).

The resulting token is refreshed automatically by the application and
written back to disk.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path

# Allow running as ``python scripts/gmail_auth.py`` from the project root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import GMAIL_SCOPES, get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorise this app to access your Gmail account.")
    parser.add_argument(
        "--client-secrets",
        default="client_secrets.json",
        help="Path to the OAuth client secrets JSON downloaded from Google Cloud.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Where to write the token (defaults to GMAIL_TOKEN_PATH from .env).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorisation URL instead of opening a browser.",
    )
    args = parser.parse_args()

    settings = get_settings()
    token_path = Path(args.token) if args.token else settings.gmail_token_path
    client_secrets = Path(args.client_secrets)

    if not client_secrets.exists():
        print(
            f"OAuth client secrets file not found: {client_secrets}\n"
            "Download it from Google Cloud:\n"
            "  APIs & Services -> Credentials -> Desktop app) and save it there.",
            file=sys.stderr,
        )
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        print(f"google-auth-oauthlib is not installed: {exc}", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), list(GMAIL_SCOPES))
    credentials = flow.run_console() if args.no_browser else flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    with suppress(OSError):  # Windows does not support POSIX file modes.
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)

    print(f"Token written to {token_path}")
    print(f"Scopes: {', '.join(credentials.scopes or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
