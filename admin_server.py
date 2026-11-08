"""
admin_server.py
~~~~~~~~~~~~~~~
Independent Web Admin portal for the StreamChat system.
Uses standard Python libraries only (http.server, sqlite3, hashlib, json).
Features:
  - View users in database users.db
  - Add new users (auto SHA-256 hashed password)
  - Update password
  - Delete user
  - Search users

Usage:
    python admin_server.py [--port 9090] [--db-path ../users.db]
"""

from __future__ import annotations

import argparse
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import sqlite3
from typing import Optional
from urllib.parse import urlparse, parse_qs

# Default location for users.db
DEFAULT_DB_FILE = Path(__file__).resolve().parent / "users.db"


class AdminDB:
    """Direct SQLite database access independent of src codebase."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

    def get_users(self, search: str = "") -> list[dict]:
        with self.get_conn() as conn:
            cursor = conn.cursor()
            if search:
                cursor.execute(
                    "SELECT id, username, created_at FROM users WHERE username LIKE ? ORDER BY id DESC;",
                    (f"%{search}%",),
                )
            else:
                cursor.execute(
                    "SELECT id, username, created_at FROM users ORDER BY id DESC;"
                )
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def add_user(self, username: str, password: str) -> tuple[bool, str]:
        username = username.strip()
        if not username or not password:
            return False, "Username and password cannot be empty."
        if len(username) < 3:
            return False, "Username must be at least 3 characters long."

        p_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        try:
            with self.get_conn() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?);",
                    (username, p_hash),
                )
                conn.commit()
            return True, f"User '{username}' created successfully!"
        except sqlite3.IntegrityError:
            return False, f"User '{username}' already exists!"
        except Exception as e:
            return False, f"Error: {e}"

    def update_password(self, user_id: int, new_password: str) -> tuple[bool, str]:
        if not new_password:
            return False, "New password cannot be empty."
        p_hash = hashlib.sha256(new_password.encode("utf-8")).hexdigest()
        try:
            with self.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?;",
                    (p_hash, user_id),
                )
                if cursor.rowcount == 0:
                    return False, "User not found."
                conn.commit()
            return True, "Password updated successfully!"
        except Exception as e:
            return False, f"Error: {e}"

    def delete_user(self, user_id: int) -> tuple[bool, str]:
        try:
            with self.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM users WHERE id = ?;", (user_id,))
                if cursor.rowcount == 0:
                    return False, "User not found."
                conn.commit()
            return True, "User deleted successfully!"
        except Exception as e:
            return False, f"Error: {e}"


# Standalone HTML Template with embedded CSS and JavaScript
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StreamChat - User Management Portal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #0b0f19;
            --bg-surface: #111827;
            --bg-elevated: #1f2937;
            --border: #374151;
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --success: #10b981;
            --danger: #ef4444;
            --text-main: #f9fafb;
            --text-muted: #9ca3af;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-base);
            color: var(--text-main);
            min-height: 100vh;
            padding: 2.5rem 1.5rem;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .container {
            width: 100%;
            max-width: 1100px;
        }

        header {
            margin-bottom: 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 1.5rem;
        }

        .logo-area {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        .logo-badge {
            width: 48px;
            height: 48px;
            background: linear-gradient(135deg, #6366f1, #8b5cf6);
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.5rem;
            font-weight: 700;
            box-shadow: 0 8px 16px rgba(99, 102, 241, 0.3);
        }

        h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .subtitle {
            font-size: 0.875rem;
            color: var(--text-muted);
            margin-top: 0.2rem;
        }

        .badge-live {
            background-color: rgba(16, 185, 129, 0.15);
            color: var(--success);
            border: 1px solid rgba(16, 185, 129, 0.3);
            padding: 0.35rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }

        .pulse {
            width: 8px;
            height: 8px;
            background-color: var(--success);
            border-radius: 50%;
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
            animation: pulse 1.8s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        .grid {
            display: grid;
            grid-template-columns: 360px 1fr;
            gap: 2rem;
        }

        @media (max-width: 860px) {
            .grid { grid-template-columns: 1fr; }
        }

        .card {
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.75rem;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.4);
        }

        .card-title {
            font-size: 1.15rem;
            font-weight: 600;
            margin-bottom: 1.25rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .form-group {
            margin-bottom: 1.25rem;
        }

        label {
            display: block;
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
        }

        input {
            width: 100%;
            padding: 0.75rem 1rem;
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text-main);
            font-size: 0.95rem;
            outline: none;
            transition: all 0.2s ease;
        }

        input:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25);
        }

        .btn {
            width: 100%;
            padding: 0.8rem 1.25rem;
            background: var(--primary);
            color: #fff;
            border: none;
            border-radius: 10px;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 0.5rem;
        }

        .btn:hover {
            background: var(--primary-hover);
            transform: translateY(-1px);
        }

        .btn-small {
            width: auto;
            padding: 0.4rem 0.75rem;
            font-size: 0.8rem;
            border-radius: 6px;
        }

        .btn-danger {
            background: rgba(239, 68, 68, 0.15);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.3);
        }
        .btn-danger:hover {
            background: var(--danger);
            color: #fff;
        }

        .table-controls {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
            gap: 1rem;
        }

        .search-box {
            position: relative;
            flex: 1;
        }

        .table-wrap {
            overflow-x: auto;
            border: 1px solid var(--border);
            border-radius: 12px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }

        th {
            background: var(--bg-elevated);
            padding: 0.85rem 1rem;
            font-weight: 600;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border);
        }

        td {
            padding: 0.85rem 1rem;
            border-bottom: 1px solid rgba(55, 65, 81, 0.5);
        }

        tr:last-child td { border-bottom: none; }

        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }

        .user-cell {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            font-weight: 600;
        }

        .avatar {
            width: 32px;
            height: 32px;
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85rem;
            color: var(--primary);
            text-transform: uppercase;
        }

        .toast {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            padding: 1rem 1.5rem;
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            display: flex;
            align-items: center;
            gap: 0.75rem;
            font-size: 0.9rem;
            transform: translateY(100px);
            opacity: 0;
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            z-index: 100;
        }

        .toast.show {
            transform: translateY(0);
            opacity: 1;
        }

        .toast.success { border-color: var(--success); color: #34d399; }
        .toast.error { border-color: var(--danger); color: #f87171; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-area">
                <div class="logo-badge">⚡</div>
                <div>
                    <h1>StreamChat User Admin</h1>
                    <div class="subtitle">Standalone Admin Portal for SQLite users.db</div>
                </div>
            </div>
            <div class="badge-live">
                <div class="pulse"></div> Database Connected
            </div>
        </header>

        <div class="grid">
            <!-- Left column: Create User Form -->
            <div class="card">
                <div class="card-title">➕ Create New Account</div>
                <form id="create-user-form">
                    <div class="form-group">
                        <label for="username">Username</label>
                        <input type="text" id="username" placeholder="e.g.: john_doe" required autocomplete="off">
                    </div>
                    <div class="form-group">
                        <label for="password">Password</label>
                        <input type="password" id="password" placeholder="Enter password..." required>
                    </div>
                    <button type="submit" class="btn" id="btn-create">Create Account</button>
                </form>
            </div>

            <!-- Right column: Users List -->
            <div class="card">
                <div class="card-title">👥 User Accounts (<span id="user-count">0</span>)</div>
                <div class="table-controls">
                    <div class="search-box">
                        <input type="text" id="search-input" placeholder="🔍 Search by username...">
                    </div>
                    <button class="btn btn-small" id="btn-refresh" style="width: auto;">Refresh</button>
                </div>

                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 70px;">ID</th>
                                <th>User</th>
                                <th>Created At</th>
                                <th style="width: 140px; text-align: right;">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="user-table-body">
                            <tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 2rem;">Loading data...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        const tableBody = document.getElementById("user-table-body");
        const userCount = document.getElementById("user-count");
        const searchInput = document.getElementById("search-input");
        const form = document.getElementById("create-user-form");
        const toast = document.getElementById("toast");

        function showToast(message, type = "success") {
            toast.textContent = message;
            toast.className = `toast show ${type}`;
            setTimeout(() => {
                toast.className = "toast";
            }, 3000);
        }

        async function fetchUsers(search = "") {
            try {
                const res = await fetch(`/api/users?search=${encodeURIComponent(search)}`);
                const data = await res.json();
                userCount.textContent = data.length;

                if (data.length === 0) {
                    tableBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 2rem;">No users found.</td></tr>`;
                    return;
                }

                tableBody.innerHTML = data.map(u => `
                    <tr>
                        <td style="color: var(--text-muted);">#${u.id}</td>
                        <td>
                            <div class="user-cell">
                                <div class="avatar">${u.username[0]}</div>
                                <span>${u.username}</span>
                            </div>
                        </td>
                        <td style="color: var(--text-muted);">${u.created_at || 'Default'}</td>
                        <td style="text-align: right;">
                            <button class="btn btn-small btn-danger" onclick="deleteUser(${u.id}, '${u.username}')">Delete</button>
                        </td>
                    </tr>
                `).join("");
            } catch (err) {
                showToast("Error loading users list", "error");
            }
        }

        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            const username = document.getElementById("username").value.trim();
            const password = document.getElementById("password").value;

            try {
                const res = await fetch("/api/users", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username, password })
                });
                const result = await res.json();
                if (result.success) {
                    showToast(result.message, "success");
                    form.reset();
                    fetchUsers(searchInput.value.trim());
                } else {
                    showToast(result.message, "error");
                }
            } catch (err) {
                showToast("Cannot connect to Admin server", "error");
            }
        });

        async function deleteUser(id, username) {
            if (!confirm(`Are you sure you want to delete user "${username}"?`)) return;
            try {
                const res = await fetch(`/api/users/${id}`, { method: "DELETE" });
                const result = await res.json();
                if (result.success) {
                    showToast(result.message, "success");
                    fetchUsers(searchInput.value.trim());
                } else {
                    showToast(result.message, "error");
                }
            } catch (err) {
                showToast("Error deleting user", "error");
            }
        }

        let debounceTimer;
        searchInput.addEventListener("input", () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                fetchUsers(searchInput.value.trim());
            }, 300);
        });

        document.getElementById("btn-refresh").addEventListener("click", () => {
            fetchUsers(searchInput.value.trim());
        });

        fetchUsers();
    </script>
</body>
</html>
"""


class AdminRequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for the Standalone Admin Portal."""

    db: AdminDB

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return

        if path == "/api/users":
            qs = parse_qs(parsed.query)
            search = qs.get("search", [""])[0]
            users = self.db.get_users(search)
            self._send_json(200, users)
            return

        self._send_json(404, {"error": "Not Found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/users":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode("utf-8")
            try:
                data = json.loads(body)
                success, msg = self.db.add_user(
                    data.get("username", ""), data.get("password", "")
                )
                self._send_json(200 if success else 400, {"success": success, "message": msg})
            except Exception as e:
                self._send_json(400, {"success": False, "message": f"Invalid data: {e}"})
            return

        self._send_json(404, {"error": "Not Found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "users":
            try:
                user_id = int(parts[2])
                success, msg = self.db.delete_user(user_id)
                self._send_json(200 if success else 400, {"success": success, "message": msg})
                return
            except ValueError:
                pass

        self._send_json(404, {"error": "Not Found"})

    def _send_json(self, status: int, data: object) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


def run_admin_portal(host: str = "127.0.0.1", port: int = 9090, db_path: Optional[Path] = None) -> None:
    db_file = db_path if db_path else DEFAULT_DB_FILE
    AdminRequestHandler.db = AdminDB(db_file)

    server = HTTPServer((host, port), AdminRequestHandler)
    print("=" * 60)
    print(f" [STREAMCHAT ADMIN] Web Portal running at:")
    print(f" -> http://{host}:{port}")
    print(f" -> Connected database: {db_file}")
    print("=" * 60)
    print("Press Ctrl+C to stop the Admin server.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STREAMCHAT ADMIN] Stopping Admin server...")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="StreamChat Standalone Admin Portal")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9090, help="Web Admin port (default 9090)")
    parser.add_argument("--db-path", type=str, default="", help="Path to users.db file")

    args = parser.parse_args()
    db_path = Path(args.db_path).resolve() if args.db_path else None
    run_admin_portal(host=args.host, port=args.port, db_path=db_path)
