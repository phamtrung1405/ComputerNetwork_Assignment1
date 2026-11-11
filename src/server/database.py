"""
database.py
~~~~~~~~~~~
User database management module using SQLite.
Supports table creation, seed sample users, adding users, and authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
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

    @staticmethod
    def hash_password_with_salt(password_token: str, salt: str) -> str:
        """Derive salted password hash using PBKDF2-HMAC-SHA256 (10,000 rounds)."""
        return hashlib.pbkdf2_hmac(
            "sha256",
            password_token.encode("utf-8"),
            salt.encode("utf-8"),
            10000,
        ).hex()

    def _init_db(self) -> None:
        """Initialize the users table and seed default sample accounts if empty."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Ensure salt column exists for existing legacy databases
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN salt TEXT DEFAULT '';")
            except sqlite3.OperationalError:
                pass

            # Check if any users exist
            cursor.execute("SELECT COUNT(*) FROM users;")
            count = cursor.fetchone()[0]

            if count == 0:
                self._seed_default_users(cursor)
            conn.commit()

    def _seed_default_users(self, cursor: sqlite3.Cursor) -> None:
        """Seed initial default sample user accounts with random salt per account."""
        sample_raw = [
            ("alice", hashlib.sha256(b"password123").hexdigest()),
            ("bob", hashlib.sha256(b"secret456").hexdigest()),
            ("charlie", hashlib.sha256(b"hello789").hexdigest()),
            ("demo", hashlib.sha256(b"demo").hexdigest()),
        ]
        # Add user1..user50
        for i in range(1, 51):
            sample_raw.append(
                (f"user{i}", hashlib.sha256(b"password123").hexdigest())
            )

        sample_users = []
        for uname, p_token in sample_raw:
            salt = secrets.token_hex(16)
            p_hash = self.hash_password_with_salt(p_token, salt)
            sample_users.append((uname, p_hash, salt))

        cursor.executemany(
            "INSERT OR IGNORE INTO users (username, password_hash, salt) VALUES (?, ?, ?);",
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
        """Verify user login credentials with constant-time comparison and salt support.

        Args:
            username: Account username.
            password_hash: SHA-256 hash token of password.

        Returns:
            True if credentials match, False otherwise.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT password_hash, salt FROM users WHERE username = ?;",
                (username,),
            )
            row = cursor.fetchone()
            if not row:
                # Constant-time dummy calculation to mitigate timing attacks
                _ = self.hash_password_with_salt(password_hash, "dummy_salt_timing")
                return False

            stored_hash = row["password_hash"]
            salt = row["salt"] if ("salt" in row.keys() and row["salt"]) else ""

            if salt:
                computed_hash = self.hash_password_with_salt(password_hash, salt)
                return hmac.compare_digest(computed_hash, stored_hash)
            else:
                # Backward-compatibility fallback for legacy unsalted databases
                matched = hmac.compare_digest(stored_hash, password_hash)
                if matched:
                    # Upgrade legacy account to salted PBKDF2 hash
                    new_salt = secrets.token_hex(16)
                    new_hash = self.hash_password_with_salt(password_hash, new_salt)
                    try:
                        cursor.execute(
                            "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?;",
                            (new_hash, new_salt, username),
                        )
                        conn.commit()
                    except Exception:
                        pass
                return matched

    def add_user(self, username: str, raw_password: str) -> bool:
        """Add a new user to the database with salt and PBKDF2 stretching.

        Args:
            username: Username.
            raw_password: Plain text password (will be SHA-256 hashed and PBKDF2 salted).

        Returns:
            True if added successfully, False if username already exists.
        """
        p_token = hashlib.sha256(raw_password.encode("utf-8")).hexdigest()
        salt = secrets.token_hex(16)
        p_hash = self.hash_password_with_salt(p_token, salt)
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?);",
                    (username, p_hash, salt),
                )
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False


# Singleton shared instance
db = UserDatabase()
