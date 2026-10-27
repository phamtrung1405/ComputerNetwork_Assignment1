"""
run_all.py
~~~~~~~~~~
Execution script for the entire StreamChat system (Server + Clients).
Automatically launches the central server and clients, opens the Web UI in browsers,
and cleanly manages shutdown of all subprocesses upon pressing Ctrl+C.

Usage:
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

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
RUN_SERVER_SCRIPT = PROJECT_ROOT / "run_server.py"
RUN_CLIENT_SCRIPT = PROJECT_ROOT / "run_client.py"

# Sample accounts list per client
SAMPLE_ACCOUNTS = [
    ("alice", "password123", "User 1"),
    ("bob", "secret456", "User 2"),
    ("charlie", "hello789", "User 3"),
    ("demo", "demo", "Quick test account"),
]
for _i in range(5, 51):
    SAMPLE_ACCOUNTS.append((f"user{_i}", "password123", f"User {_i}"))

# Child processes list
child_processes: List[subprocess.Popen] = []


def cleanup_processes() -> None:
    """Clean up and terminate all child processes upon exit."""
    if not child_processes:
        return
    print("\n[SYSTEM] Stopping all child processes...")
    for proc in child_processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    # Wait for processes to exit
    time.sleep(0.5)
    for proc in child_processes:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    print("[SYSTEM] Successfully shut down all Server and Client processes.")


def handle_exit(signum, frame) -> None:
    """Handle interrupt signal (Ctrl+C)."""
    cleanup_processes()
    sys.exit(0)


def check_all_python_files() -> bool:
    """Validate syntax and compilation of all Python files in the project."""
    print("=" * 60)
    print("  PYTHON SYNTAX AND COMPILATION VALIDATION")
    print("=" * 60)

    src_dir = PROJECT_ROOT / "src"
    py_files = sorted(list(PROJECT_ROOT.glob("*.py")) + list(src_dir.rglob("*.py")))

    has_error = False
    for py_file in py_files:
        rel_path = py_file.relative_to(PROJECT_ROOT)
        try:
            # Check syntax using py_compile
            subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"  [OK] {rel_path}")
        except subprocess.CalledProcessError as err:
            has_error = True
            print(f"  [ERROR] {rel_path}:\n{err.stderr}")

    print("-" * 60)
    if not has_error:
        print("[SUCCESS] All Python files validated with no syntax errors!")
    else:
        print("[FAILURE] Errors detected in Python source files.")
    print("=" * 60)
    return not has_error


def check_dependencies() -> None:
    """Verify required dependencies are installed."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("[WARNING] Missing dependency 'cryptography'.")
        print("Installing from requirements.txt...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")],
            check=True,
        )
        print("[SUCCESS] Dependencies installed successfully.\n")


def main() -> None:
    """Main entry point to launch entire system."""
    parser = argparse.ArgumentParser(
        description="StreamChat — Launch entire system (Server & Clients)"
    )
    parser.add_argument(
        "--clients",
        type=int,
        default=2,
        help="Number of browser tabs to open for testing (default: 2 for Alice and Bob)",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=9000,
        help="Server UDP port (default: 9000)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8080,
        help="Unified Web UI and SSE port (default: 8080)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open web browser",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only validate syntax and compilation of Python files and exit",
    )
    args = parser.parse_args()

    # Syntax check mode
    if args.check:
        success = check_all_python_files()
        sys.exit(0 if success else 1)

    # Register exit handlers
    atexit.register(cleanup_processes)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # 1. Environment check
    check_dependencies()

    print("=" * 65)
    print("        STREAMCHAT — SYSTEM LAUNCH ON PORT 8080")
    print("=" * 65)

    # 2. Launch Server
    print(f"\n[1/2] Starting Server on Web port {args.http_port} & UDP port {args.server_port}...")
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

    # Wait for server socket readiness
    time.sleep(1.0)
    if server_proc.poll() is not None:
        print("[ERROR] Server startup failed!")
        sys.exit(1)

    web_url = f"http://127.0.0.1:{args.http_port}"

    # 3. Display setup guide
    print("\n" + "=" * 65)
    print("  SYSTEM READY ON PORT 8080!")
    print("=" * 65)
    print(f"  * Web UI & SSE Address:        {web_url}")
    print(f"  * UDP Transport Port:          127.0.0.1:{args.server_port}")
    print("  * Each browser tab acts as an independent client.")
    print("  * You can open multiple tabs or access from LAN devices:")
    for idx in range(min(args.clients, len(SAMPLE_ACCOUNTS))):
        acc = SAMPLE_ACCOUNTS[idx]
        print(f"    - Tab {idx + 1}: Username: {acc[0]} | Password: {acc[1]} ({acc[2]})")
    print("-" * 65)
    print("  [TIP] Press Ctrl + C to safely shut down the system.")
    print("=" * 65 + "\n")

    # 4. Open browser if not disabled
    if not args.no_browser:
        max_open = min(args.clients, 4)
        print(f"[2/2] Opening {max_open} browser tab(s) to {web_url}...")
        for i in range(max_open):
            webbrowser.open(web_url)
            time.sleep(0.4)
        if args.clients > 4:
            print(f"[NOTE] Only opening {max_open} initial tabs to avoid freezing the browser.")

    # 5. Monitor subprocesses
    try:
        while True:
            for proc in child_processes:
                if proc.poll() is not None:
                    print("\n[WARNING] A child process has terminated. Cleaning up...")
                    cleanup_processes()
                    sys.exit(0)
            time.sleep(1.0)
    except KeyboardInterrupt:
        cleanup_processes()
        sys.exit(0)


if __name__ == "__main__":
    main()
