"""
run_server.py
~~~~~~~~~~~~~
Launcher script for StreamChat central server:
- Web UI & SSE Server: Listens on port 8080 (Standard Client-Server HTTP & SSE).
- UDP Transport Engine: Listens on UDP port 9000 (Compliant with distributed transport protocol).

Usage:
    python run_server.py [--host HOST] [--port UDP_PORT] [--web-port WEB_PORT]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading

# Add src folder to sys.path to import modules
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from common.constants import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, DEFAULT_HTTP_PORT
from server.server import ChatServer
from server.web_server import UnifiedChatServer


def main() -> None:
    """Entry point for server."""
    parser = argparse.ArgumentParser(description="StreamChat Unified Server")
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_SERVER_HOST,
        help=f"Listening IP address (default: {DEFAULT_SERVER_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help=f"UDP port (default: {DEFAULT_SERVER_PORT})",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Web UI and SSE port (default: {DEFAULT_HTTP_PORT})",
    )
    args = parser.parse_args()

    print("===============================================================")
    print("                 STREAMCHAT CENTRAL SERVER")
    print("===============================================================")
    print(f"  * Web UI & SSE Server:    http://127.0.0.1:{args.web_port}")
    print(f"  * UDP Transport Engine:   127.0.0.1:{args.port} (UDP)")
    print(f"  * Supports multiple accounts on port {args.web_port}")
    print("===============================================================")

    # 1. Start UDP ChatServer in a background thread
    udp_server = ChatServer()
    udp_thread = threading.Thread(
        target=udp_server.start_server,
        kwargs={"host": args.host, "port": args.port},
        daemon=True,
    )
    udp_thread.start()

    # 2. Start Web & SSE Server on port 8080 (blocking)
    web_server = UnifiedChatServer(host="0.0.0.0", port=args.web_port)
    try:
        web_server.start()
    except KeyboardInterrupt:
        print("\n[SYSTEM] StreamChat Server stopped.")
        web_server.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
