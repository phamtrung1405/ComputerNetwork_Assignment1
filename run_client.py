"""
run_client.py
~~~~~~~~~~~~~
Script to open the Client user interface in a web browser.
In the Client-Server HTTP & SSE architecture on port 8080, all clients
access http://<server_ip>:8080 to log into independent accounts.

Usage:
    python run_client.py [--server-ip IP] [--http-port 8080] [--no-browser]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import webbrowser

# Add src folder to sys.path to import modules
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from common.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_HTTP_PORT,
)


def main() -> None:
    """Entry point for client launcher."""
    parser = argparse.ArgumentParser(description="StreamChat Client Launcher")
    parser.add_argument(
        "--server-ip",
        type=str,
        default=DEFAULT_SERVER_HOST,
        help=f"StreamChat Server IP (default: {DEFAULT_SERVER_HOST})",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Web UI and SSE port (default: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open web browser",
    )
    args = parser.parse_args()

    url = f"http://{args.server_ip}:{args.http_port}"
    print("===============================================================")
    print("                    STREAMCHAT CLIENT")
    print("===============================================================")
    print(f"  * Web UI URL (HTTP & SSE): {url}")
    print("  * You can open multiple browser tabs at this address")
    print("    to log into different accounts simultaneously.")
    print("===============================================================")

    if not args.no_browser:
        print(f"\nOpening browser to {url}...")
        webbrowser.open(url)
    else:
        print(f"\nPlease open your browser and navigate to: {url}")


if __name__ == "__main__":
    main()
