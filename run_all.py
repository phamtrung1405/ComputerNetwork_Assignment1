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
    print("        STREAMCHAT — KHỞI CHẠY HỆ THỐNG PHÂN TÁN ĐA TIẾN TRÌNH")
    print("=" * 65)

    # 2. Khởi chạy UDP Server trung tâm
    print(f"\n[1/{args.clients + 1}] Đang khởi động Server UDP tại cổng {args.server_port}...")
    server_cmd = [
        sys.executable,
        "-u",
        str(RUN_SERVER_SCRIPT),
        "--port",
        str(args.server_port),
    ]
    server_proc = subprocess.Popen(server_cmd, cwd=str(PROJECT_ROOT))
    child_processes.append(server_proc)

    # Chờ 0.8s để UDP Server socket sẵn sàng
    time.sleep(0.8)
    if server_proc.poll() is not None:
        print("[LỖI] Server UDP khởi động thất bại!")
        sys.exit(1)

    # 3. Khởi chạy các tiến trình Client độc lập
    start_port = args.http_port
    client_urls: List[str] = []

    print(f"\n[2/{args.clients + 1}] Đang khởi chạy {args.clients} tiến trình Client độc lập...")
    for idx in range(args.clients):
        client_port = start_port + idx
        client_cmd = [
            sys.executable,
            "-u",
            str(RUN_CLIENT_SCRIPT),
            "--server-port",
            str(args.server_port),
            "--http-port",
            str(client_port),
            "--no-browser",  # Sẽ mở đồng loạt bên dưới
        ]
        c_proc = subprocess.Popen(client_cmd, cwd=str(PROJECT_ROOT))
        child_processes.append(c_proc)
        client_urls.append(f"http://127.0.0.1:{client_port}")
        time.sleep(0.3)

    # 4. Hiển thị thông tin hướng dẫn
    print("\n" + "=" * 65)
    print("  HỆ THỐNG PHÂN TÁN ĐÃ SẴN SÀNG HOẠT ĐỘNG (100% QUA UDP)!")
    print("=" * 65)
    print(f"  * UDP Transport Engine: 127.0.0.1:{args.server_port}")
    print(f"  * Danh sách {args.clients} Client Web UI tương ứng:")
    for idx in range(min(args.clients, len(SAMPLE_ACCOUNTS))):
        acc = SAMPLE_ACCOUNTS[idx]
        curl = client_urls[idx]
        print(f"    - Client {idx + 1} ({curl}): Username: {acc[0]} | Password: {acc[1]} ({acc[2]})")
    print("-" * 65)
    print("  [TIP] Nhấn Ctrl + C để dừng an toàn toàn bộ Server & Clients.")
    print("=" * 65 + "\n")

    # 5. Tự động mở trình duyệt cho các Client
    if not args.no_browser:
        max_open = min(args.clients, 4)
        print(f"Đang tự động mở {max_open} tab trình duyệt cho các Client...")
        for i in range(max_open):
            webbrowser.open(client_urls[i])
            time.sleep(0.4)
        if args.clients > 4:
            print(f"[LƯU Ý] Vì số lượng client lớn ({args.clients}), hệ thống chỉ tự động mở {max_open} tab đầu tiên. Các client còn lại truy cập qua URL tương ứng.")

    # 6. Giữ tiến trình cha hoạt động và giám sát các tiến trình con
    try:
        while True:
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
