"""
database.py
~~~~~~~~~~~
User database management module using SQLite.
Supports table creation, seed sample users, adding users, and authentication.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Optional


# Default path to users.db in the project root directory
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "users.db"


class UserDatabase:
    """Manages SQLite connections and queries for user authentication."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a connection to the SQLite database."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize the users table and seed default sample accounts if empty."""
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

            # Check if any users exist
            cursor.execute("SELECT COUNT(*) FROM users;")
            count = cursor.fetchone()[0]

            if count == 0:
                self._seed_default_users(cursor)
            conn.commit()

    def _seed_default_users(self, cursor: sqlite3.Cursor) -> None:
        """Seed initial default sample user accounts."""
        sample_users = [
            ("alice", hashlib.sha256(b"password123").hexdigest()),
            ("bob", hashlib.sha256(b"secret456").hexdigest()),
            ("charlie", hashlib.sha256(b"hello789").hexdigest()),
            ("demo", hashlib.sha256(b"demo").hexdigest()),
        ]
        # Add user1..user50
        for i in range(1, 51):
            sample_users.append(
                (f"user{i}", hashlib.sha256(b"password123").hexdigest())
            )

        cursor.executemany(
            "INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, ?);",
            sample_users,
        )

    def get_password_hash(self, username: str) -> Optional[str]:
        """Get password_hash for a given username.

        Args:
            username: Account username.

        Returns:
            password_hash if user exists, otherwise None.
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
        """Verify user login credentials.

        Args:
            username: Account username.
            password_hash: SHA-256 hash of password.

        Returns:
            True if credentials match, False otherwise.
        """
        expected = self.get_password_hash(username)
        return expected is not None and expected == password_hash

    def add_user(self, username: str, raw_password: str) -> bool:
        """Add a new user to the database.

        Args:
            username: Username.
            raw_password: Plain text password (will be SHA-256 hashed).

        Returns:
            True if added successfully, False if username already exists.
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


# Singleton shared instance
db = UserDatabase()
