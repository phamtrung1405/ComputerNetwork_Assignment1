"""
run_client.py
~~~~~~~~~~~~~
Script mở giao diện người dùng Client trên trình duyệt web.
Trong kiến trúc Client-Server HTTP & SSE trên port 8080, tất cả client
truy cập cùng địa chỉ http://<server_ip>:8080 để đăng nhập tài khoản độc lập.

Sử dụng:
    python run_client.py [--server-ip IP] [--http-port 8080] [--no-browser]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import webbrowser

# Thêm thư mục src vào sys.path để import các module
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Đảm bảo UTF-8 output trên Windows console
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
    """Entry point cho client."""
    parser = argparse.ArgumentParser(description="StreamChat Client Launcher")
    parser.add_argument(
        "--server-ip",
        type=str,
        default=DEFAULT_SERVER_HOST,
        help=f"IP máy chủ StreamChat (mặc định: {DEFAULT_SERVER_HOST})",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Cổng Web UI và SSE (mặc định: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Không tự động mở trình duyệt web",
    )
    args = parser.parse_args()

    url = f"http://{args.server_ip}:{args.http_port}"
    print("===============================================================")
    print("                    STREAMCHAT CLIENT")
    print("===============================================================")
    print(f"  * Dia chi ket noi Web UI (HTTP & SSE): {url}")
    print("  * Ban co the mo nhieu tab trinh duyet tren cung dia chi nay")
    print("    de dang nhap dong thoi nhieu tai khoan khac nhau.")
    print("===============================================================")

    if not args.no_browser:
        print(f"\nDang mo trinh duyet toi {url}...")
        webbrowser.open(url)
    else:
        print(f"\nVui long mo trinh duyet va truy cap: {url}")


if __name__ == "__main__":
    main()
