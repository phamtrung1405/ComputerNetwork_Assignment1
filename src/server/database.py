"""
database.py
~~~~~~~~~~~
Module quản lý cơ sở dữ liệu người dùng bằng SQLite.
Hỗ trợ tạo bảng, khởi tạo dữ liệu mẫu, thêm người dùng và xác thực.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Optional


# Đường dẫn mặc định tới file users.db trong thư mục dự án
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "users.db"


class UserDatabase:
    """Quản lý kết nối và truy vấn SQLite cho người dùng."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Tạo kết nối đến SQLite database."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Khởi tạo bảng users và nạp tài khoản mẫu ban đầu nếu bảng rỗng."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Kiểm tra xem đã có dữ liệu chưa
            cursor.execute("SELECT COUNT(*) FROM users;")
            count = cursor.fetchone()[0]

            if count == 0:
                self._seed_default_users(cursor)
            conn.commit()

    def _seed_default_users(self, cursor: sqlite3.Cursor) -> None:
        """Nạp các tài khoản mẫu ban đầu."""
        sample_users = [
            ("alice", hashlib.sha256(b"password123").hexdigest()),
            ("bob", hashlib.sha256(b"secret456").hexdigest()),
            ("charlie", hashlib.sha256(b"hello789").hexdigest()),
            ("demo", hashlib.sha256(b"demo").hexdigest()),
        ]
        # Thêm user1..user50
        for i in range(1, 51):
            sample_users.append(
                (f"user{i}", hashlib.sha256(b"password123").hexdigest())
            )

        cursor.executemany(
            "INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, ?);",
            sample_users,
        )

    def get_password_hash(self, username: str) -> Optional[str]:
        """Lấy password_hash của một username.

        Args:
            username: Tên tài khoản.

        Returns:
            password_hash nếu tồn tại, ngược lại None.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT password_hash FROM users WHERE username = ?;",
                (username,),
            )
            row = cursor.fetchone()
            if row:
                return row["password_hash"]
        return None

    def verify_user(self, username: str, password_hash: str) -> bool:
        """Kiểm tra thông tin đăng nhập.

        Args:
            username: Tên tài khoản.
            password_hash: Mã băm SHA-256 của mật khẩu.

        Returns:
            True nếu hợp lệ, ngược lại False.
        """
        expected = self.get_password_hash(username)
        return expected is not None and expected == password_hash

    def add_user(self, username: str, raw_password: str) -> bool:
        """Thêm người dùng mới vào database (tiện ích mở rộng).

        Args:
            username: Tên người dùng.
            raw_password: Mật khẩu dạng rõ (sẽ được hash SHA-256).

        Returns:
            True nếu thêm thành công, False nếu username đã tồn tại.
        """
        p_hash = hashlib.sha256(raw_password.encode("utf-8")).hexdigest()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?);",
                    (username, p_hash),
                )
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False


# Singleton instance dùng chung trong server
db = UserDatabase()
