"""
run_server.py
~~~~~~~~~~~~~
Script khởi chạy máy chủ trung tâm StreamChat (UDP Transport Engine).

Sử dụng:
    python run_server.py [--host HOST] [--port PORT]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Thêm thư mục src vào sys.path để import các module
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Đảm bảo UTF-8 output trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from common.constants import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT
from server.server import ChatServer


def main() -> None:
    """Entry point cho server."""
    parser = argparse.ArgumentParser(description="StreamChat Central UDP Server")
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_SERVER_HOST,
        help=f"Địa chỉ IP lắng nghe (mặc định: {DEFAULT_SERVER_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help=f"Cổng UDP (mặc định: {DEFAULT_SERVER_PORT})",
    )
    args = parser.parse_args()

    print("===============================================================")
    print("                 STREAMCHAT CENTRAL SERVER")
    print("===============================================================")
    print(f"  * UDP Transport Engine:   {args.host}:{args.port} (UDP)")
    print("  * Khung giao thức tùy chỉnh: 28B Header, AES-GCM, CUBIC/BBR")
    print("  * Sẵn sàng phục vụ các Client kết nối phân tán từ xa")
    print("===============================================================")

    server = ChatServer()
    try:
        server.start_server(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\n[HỆ THỐNG] Máy chủ StreamChat đã dừng.")
        sys.exit(0)


if __name__ == "__main__":
    main()

