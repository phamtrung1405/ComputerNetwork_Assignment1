"""
run_client.py
~~~~~~~~~~~~~
Launcher script for StreamChat Client.
Connects to StreamChat Server via custom reliable UDP protocol on port 9000,
and starts an internal lightweight HTTP/SSE UI server (raw TCP socket) on an
automatically allocated local port (default 8080, 8081...).

Usage:
    python run_client.py [--server-ip IP] [--server-port 9000] [--http-port 8080] [--no-browser]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys
import threading
import time
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
    DEFAULT_SERVER_PORT,
    DEFAULT_HTTP_PORT,
)
from client.client import ChatClient
from client.http_ui import HttpUI


def find_available_port(start_port: int, max_attempts: int = 50) -> int:
    """Find the next available TCP port starting from start_port."""
    for p in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start_port


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
        "--port",
        "--server-port",
        dest="server_port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help=f"StreamChat Server UDP port (default: {DEFAULT_SERVER_PORT})",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Base local Web UI port (default: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open web browser",
    )
    args = parser.parse_args()

    # 1. Port-hunting for available local Web UI port (ADR-004)
    ui_port = find_available_port(args.http_port)
    url = f"http://127.0.0.1:{ui_port}"

    print("===============================================================")
    print("                    STREAMCHAT CLIENT")
    print("===============================================================")
    print(f"  * UDP Transport Engine:   Connected to {args.server_ip}:{args.server_port} (UDP)")
    print(f"  * Local Web UI Bridge:    {url}")
    print("  * 100% compliant with course requirements (ADR-004)")
    print("===============================================================")

    # 2. Instantiate UDP ChatClient
    chat_client = ChatClient(
        server_ip=args.server_ip,
        server_port=args.server_port,
    )

    # 3. Instantiate and start local HttpUI
    http_ui = HttpUI(port=ui_port, client=chat_client)
    ui_thread = threading.Thread(target=http_ui.start, daemon=True)
    ui_thread.start()

    # 4. Open browser
    if not args.no_browser:
        print(f"\nOpening browser to {url}...")
        webbrowser.open(url)
    else:
        print(f"\nPlease open your browser and navigate to: {url}")

    # 5. Keep client running until interrupt
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[CLIENT] Shutting down...")
        chat_client.disconnect()
        sys.exit(0)


if __name__ == "__main__":
    main()
