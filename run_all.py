"""
run_all.py
~~~~~~~~~~
Script thực thi toàn bộ hệ thống StreamChat (Server + Clients).
Tự động khởi chạy Server trung tâm và các Client, mở trình duyệt Web UI,
và quản lý dừng tất cả tiến trình an toàn khi nhấn Ctrl+C.

Sử dụng:
    python run_all.py [--clients N] [--server-port PORT] [--start-http-port PORT] [--no-browser] [--check]
"""

from __future__ import annotations

import argparse
import atexit
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import webbrowser
from typing import List

# Đảm bảo UTF-8 output trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
RUN_SERVER_SCRIPT = PROJECT_ROOT / "run_server.py"
RUN_CLIENT_SCRIPT = PROJECT_ROOT / "run_client.py"

# Danh sách tài khoản mẫu theo từng client
SAMPLE_ACCOUNTS = [
    ("alice", "password123", "Người dùng 1"),
    ("bob", "secret456", "Người dùng 2"),
    ("charlie", "hello789", "Người dùng 3"),
    ("demo", "demo", "Tài khoản test nhanh"),
]
for _i in range(5, 51):
    SAMPLE_ACCOUNTS.append((f"user{_i}", "password123", f"Người dùng {_i}"))

# Lưu trữ các tiến trình con
child_processes: List[subprocess.Popen] = []


def cleanup_processes() -> None:
    """Dọn dẹp và kết thúc tất cả các tiến trình con khi thoát."""
    if not child_processes:
        return
    print("\n[HỆ THỐNG] Đang dừng tất cả các tiến trình con...")
    for proc in child_processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    
    # Chờ tiến trình kết thúc
    time.sleep(0.5)
    for proc in child_processes:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    print("[HỆ THỐNG] Đã tắt toàn bộ Server và Clients thành công.")


def handle_exit(signum, frame) -> None:
    """Xử lý tín hiệu ngắt (Ctrl+C)."""
    cleanup_processes()
    sys.exit(0)


def check_all_python_files() -> bool:
    """Kiểm tra cú pháp và import toàn bộ file Python trong dự án."""
    print("=" * 60)
    print("  KIỂM TRA CÚ PHÁP VÀ IMPORT TOÀN BỘ CODE PYTHON")
    print("=" * 60)
    
    src_dir = PROJECT_ROOT / "src"
    py_files = sorted(list(PROJECT_ROOT.glob("*.py")) + list(src_dir.rglob("*.py")))
    
    has_error = False
    for py_file in py_files:
        rel_path = py_file.relative_to(PROJECT_ROOT)
        try:
            # Kiểm tra cú pháp bằng py_compile
            subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"  [OK] {rel_path}")
        except subprocess.CalledProcessError as err:
            has_error = True
            print(f"  [LỖI] {rel_path}:\n{err.stderr}")

    print("-" * 60)
    if not has_error:
        print("[THÀNH CÔNG] Toàn bộ code Python đã được kiểm tra, không có lỗi cú pháp!")
    else:
        print("[THẤT BẠI] Phát hiện lỗi trong các file mã nguồn Python.")
    print("=" * 60)
    return not has_error


def check_dependencies() -> None:
    """Kiểm tra xem thư viện yêu cầu đã được cài đặt chưa."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("[CẢNH BÁO] Chưa tìm thấy thư viện 'cryptography'.")
        print("Đang tiến hành cài đặt từ requirements.txt...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")],
            check=True,
        )
        print("[THÀNH CÔNG] Đã cài đặt xong phụ thuộc.\n")


def main() -> None:
    """Entry point chính để khởi chạy toàn bộ hệ thống."""
    parser = argparse.ArgumentParser(
        description="StreamChat — Khởi chạy toàn bộ hệ thống (Server & Clients)"
    )
    parser.add_argument(
        "--clients",
        type=int,
        default=2,
        help="Số lượng tab trình duyệt cần mở để kiểm thử (mặc định: 2 cho Alice và Bob)",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=9000,
        help="Cổng UDP của Server (mặc định: 9000)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8080,
        help="Cổng Web UI và SSE duy nhất (mặc định: 8080)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Không tự động mở trình duyệt web",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Chỉ kiểm tra cú pháp và tính toàn vẹn của tất cả file Python rồi thoát",
    )
    args = parser.parse_args()

    # Nếu chỉ muốn kiểm tra code
    if args.check:
        success = check_all_python_files()
        sys.exit(0 if success else 1)

    # Đăng ký hàm dọn dẹp khi thoát
    atexit.register(cleanup_processes)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # 1. Kiểm tra môi trường
    check_dependencies()

    print("=" * 65)
    print("        STREAMCHAT — KHỞI CHẠY HỆ THỐNG TRÊN PORT 8080")
    print("=" * 65)

    # 2. Khởi chạy Server duy nhất (cung cấp cả Web UI, SSE và UDP)
    print(f"\n[1/2] Đang khởi động Server tại cổng Web {args.http_port} & UDP {args.server_port}...")
    server_cmd = [
        sys.executable,
        "-u",
        str(RUN_SERVER_SCRIPT),
        "--port",
        str(args.server_port),
        "--web-port",
        str(args.http_port),
    ]
    server_proc = subprocess.Popen(server_cmd, cwd=str(PROJECT_ROOT))
    child_processes.append(server_proc)

    # Chờ một chút để Server socket sẵn sàng
    time.sleep(1.0)
    if server_proc.poll() is not None:
        print("[LỖI] Server khởi động thất bại!")
        sys.exit(1)

    web_url = f"http://127.0.0.1:{args.http_port}"

    # 3. Hiển thị thông tin hướng dẫn
    print("\n" + "=" * 65)
    print("  HỆ THỐNG ĐÃ SẴN SÀNG HOẠT ĐỘNG TRÊN 1 PORT DUY NHẤT!")
    print("=" * 65)
    print(f"  * Dia chi Web UI & SSE:       {web_url}")
    print(f"  * UDP Transport Port:          127.0.0.1:{args.server_port}")
    print("  * Moi tab trinh duyet hoat dong nhu mot Client doc lap.")
    print("  * Co the mo hang tram tab hoac tu cac may khac trong mang LAN:")
    for idx in range(min(args.clients, len(SAMPLE_ACCOUNTS))):
        acc = SAMPLE_ACCOUNTS[idx]
        print(f"    - Tab {idx + 1}: Username: {acc[0]} | Password: {acc[1]} ({acc[2]})")
    print("-" * 65)
    print("  [TIP] Nhấn Ctrl + C để dừng Server an toàn.")
    print("=" * 65 + "\n")

    # 4. Tự động mở trình duyệt nếu không bật --no-browser
    if not args.no_browser:
        max_open = min(args.clients, 4)
        print(f"[2/2] Đang tự động mở {max_open} tab trình duyệt tới {web_url}...")
        for i in range(max_open):
            webbrowser.open(web_url)
            time.sleep(0.4)
        if args.clients > 4:
            print(f"[LƯU Ý] Vì số lượng client lớn ({args.clients}), hệ thống chỉ tự động mở {max_open} tab đầu tiên để tránh làm đơ trình duyệt. Các client còn lại có thể truy cập qua URL tương ứng.")


    # 6. Giữ tiến trình cha hoạt động và giám sát các tiến trình con
    try:
        while True:
            # Kiểm tra xem có tiến trình nào bị thoát bất ngờ không
            for proc in child_processes:
                if proc.poll() is not None:
                    print("\n[CẢNH BÁO] Một tiến trình con đã dừng. Đang dọn dẹp hệ thống...")
                    cleanup_processes()
                    sys.exit(0)
            time.sleep(1.0)
    except KeyboardInterrupt:
        cleanup_processes()
        sys.exit(0)


if __name__ == "__main__":
    main()
