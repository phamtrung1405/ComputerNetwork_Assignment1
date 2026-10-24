/**
 * StreamChat — Client-side Application Logic
 *
 * Handles:
 * - Login flow via POST /api/login
 * - Server-Sent Events (SSE) for real-time updates
 * - Chat message sending via POST /api/send
 * - File transfer initiation via POST /api/file
 * - Progress bar rendering for file transfers
 * - Message rendering and auto-scroll
 */

"use strict";

// ═══════════════════════════════════════════════
// DOM REFERENCES
// ═══════════════════════════════════════════════

const loginScreen = document.getElementById("login-screen");
const chatScreen = document.getElementById("chat-screen");
const loginForm = document.getElementById("login-form");
const loginUsername = document.getElementById("login-username");
const loginPassword = document.getElementById("login-password");
const loginBtn = document.getElementById("login-btn");
const loginError = document.getElementById("login-error");
const displayUsername = document.getElementById("display-username");
const userAvatar = document.getElementById("user-avatar");
const messagesContainer = document.getElementById("messages-container");
const messagesDiv = document.getElementById("messages");
const msgInput = document.getElementById("msg-input");
const btnSend = document.getElementById("btn-send");
const btnFile = document.getElementById("btn-file");
const btnDisconnect = document.getElementById("btn-disconnect");
const connectionStatus = document.getElementById("connection-status");
const fileDialog = document.getElementById("file-dialog");
const filePathInput = document.getElementById("file-path-input");
const fileTargetInput = document.getElementById("file-target-input");
const fileSendBtn = document.getElementById("file-send-btn");
const fileCancelBtn = document.getElementById("file-cancel-btn");
const fileTransfersDiv = document.getElementById("file-transfers");

// ═══════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════

let currentUsername = "";
let eventSource = null;
let isConnected = false;

// ═══════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════

/**
 * Format a UNIX timestamp to a human-readable time string.
 * @param {number} ts - UNIX timestamp in seconds.
 * @returns {string} Formatted time string (HH:MM).
 */
function formatTime(ts) {
    const d = new Date(ts * 1000);
    const h = d.getHours().toString().padStart(2, "0");
    const m = d.getMinutes().toString().padStart(2, "0");
    return `${h}:${m}`;
}

/**
 * Format bytes to a human-readable string.
 * @param {number} bytes - Number of bytes.
 * @returns {string} Formatted string (e.g. "1.5 MB").
 */
function formatBytes(bytes) {
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(1) + " " + units[i];
}

/**
 * Send a fetch request to the local HTTP server.
 * @param {string} url - The API endpoint.
 * @param {string} method - HTTP method.
 * @param {Object} [body] - Request body (JSON).
 * @returns {Promise<Object>} Parsed JSON response.
 */



async function apiCall(url, method, body) {
    const options = {
        method: method,
        headers: { "Content-Type": "application/json" },
    };
    if (body) {
        options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    return response.json();
}

/**
 * Auto-scroll messages container to the bottom.
 */
function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
    });
}

// ═══════════════════════════════════════════════
// LOGIN
// ═══════════════════════════════════════════════

loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = loginUsername.value.trim();
    const password = loginPassword.value.trim();

    if (!username || !password) return;

    // Show spinner
    loginBtn.querySelector(".btn-text").textContent = "Đang xác thực...";
    loginBtn.querySelector(".btn-spinner").classList.remove("hidden");
    loginBtn.disabled = true;
    loginError.classList.add("hidden");

    try {
        currentUsername = username;
        // Đảm bảo SSE đã được bật để nhận kết quả xác thực AUTH_RESP
        startSSE();

        const result = await apiCall("/api/login", "POST", { username, password });

        if (result.status === "error") {
            showLoginError(result.message);
            return;
        }

        // Đợi kết quả thực sự từ Server qua AUTH_RESP (không tự ý chuyển màn hình trước)
    } catch (err) {
        showLoginError("Không thể kết nối đến server. Hãy đảm bảo server đang chạy.");
    }
});

/**
 * Display a login error message.
 * @param {string} msg - Error message to display.
 */
function showLoginError(msg) {
    loginError.textContent = msg;
    loginError.classList.remove("hidden");
    loginBtn.querySelector(".btn-text").textContent = "Đăng nhập";
    loginBtn.querySelector(".btn-spinner").classList.add("hidden");
    loginBtn.disabled = false;
}

// ═══════════════════════════════════════════════
// SERVER-SENT EVENTS (SSE)
// ═══════════════════════════════════════════════

/**
 * Start the SSE connection to receive real-time events from the server.
 */
function startSSE() {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource("/events");

    eventSource.onopen = () => {
        connectionStatus.textContent = "● Đã kết nối";
        connectionStatus.className = "conn-status connected";
    };

    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleServerEvent(data);
        } catch (err) {
            console.warn("SSE parse error:", err);
        }
    };

    eventSource.onerror = () => {
        connectionStatus.textContent = "● Mất kết nối";
        connectionStatus.className = "conn-status disconnected";
        // Auto-reconnect is built into EventSource
    };
}

/**
 * Handle an incoming event from the server.
 * @param {Object} data - Parsed event data.
 */
function handleServerEvent(data) {
    const type = data.type;

    switch (type) {
        case "AUTH_RESP":
            handleAuthResponse(data);
            break;
        case "MSG":
            handleChatMessage(data);
            break;
        case "SYSTEM":
            handleSystemMessage(data);
            break;
        case "FILE_START":
            handleFileStart(data);
            break;
        case "FILE_COMPLETE":
            handleFileComplete(data);
            break;
        case "ERROR":
            handleErrorMessage(data);
            break;
        case "DISCONNECTED":
            handleDisconnect();
            break;
        default:
            console.log("Unknown event type:", type, data);
    }
}

/**
 * Handle authentication response.
 * @param {Object} data - Auth response data.
 */
function handleAuthResponse(data) {
    if (data.success) {
        // Chuyển sang màn hình chat khi xác thực thành công thực sự
        displayUsername.textContent = currentUsername;
        userAvatar.textContent = currentUsername.charAt(0).toUpperCase();
        loginScreen.classList.remove("active");
        chatScreen.classList.add("active");
        isConnected = true;
        msgInput.focus();

        addSystemMessage(data.message || "Đăng nhập thành công!");
    } else {
        // Đăng nhập thất bại: giữ nguyên ở màn hình login và báo lỗi rõ ràng
        showLoginError(data.message || "Tên đăng nhập hoặc mật khẩu không đúng.");
        isConnected = false;
    }
}

/**
 * Handle incoming chat message.
 * @param {Object} data - Message data with from, text, timestamp fields.
 */
function handleChatMessage(data) {
    const isOutgoing = data.from === currentUsername;
    addChatMessage(data.from, data.text, data.timestamp, isOutgoing);
}

/**
 * Handle system message.
 * @param {Object} data - System message data.
 */
function handleSystemMessage(data) {
    addSystemMessage(data.message);
}

/**
 * Handle file transfer start notification.
 * @param {Object} data - File start data.
 */
function handleFileStart(data) {
    addSystemMessage(`📁 ${data.from} đang gửi file: ${data.filename} (${formatBytes(data.size)})`);
    addFileTransfer(data.stream_id, data.filename, data.size);
}

/**
 * Handle file transfer completion.
 * @param {Object} data - File complete data.
 */
function handleFileComplete(data) {
    addSystemMessage(`✅ File ${data.filename} đã truyền xong!`);
    updateFileTransferStatus(data.stream_id, "completed");
}

/**
 * Handle error message.
 * @param {Object} data - Error data.
 */
function handleErrorMessage(data) {
    addSystemMessage("⚠️ " + data.message);
}

/**
 * Handle disconnection.
 */
function handleDisconnect() {
    isConnected = false;
    connectionStatus.textContent = "● Đã ngắt kết nối";
    connectionStatus.className = "conn-status disconnected";
    addSystemMessage("🔌 Đã ngắt kết nối khỏi server.");
}

// ═══════════════════════════════════════════════
// MESSAGE RENDERING
// ═══════════════════════════════════════════════

/**
 * Add a chat message to the message area.
 * @param {string} sender - Username of the sender.
 * @param {string} text - Message text.
 * @param {number} timestamp - UNIX timestamp.
 * @param {boolean} isOutgoing - True if sent by current user.
 */
function addChatMessage(sender, text, timestamp, isOutgoing) {
    const msgEl = document.createElement("div");
    msgEl.className = `message ${isOutgoing ? "outgoing" : "incoming"}`;

    const senderEl = document.createElement("div");
    senderEl.className = "message-sender";
    senderEl.textContent = sender;

    const bubbleEl = document.createElement("div");
    bubbleEl.className = "message-bubble";
    bubbleEl.textContent = text;

    const timeEl = document.createElement("div");
    timeEl.className = "message-time";
    timeEl.textContent = timestamp ? formatTime(timestamp) : formatTime(Date.now() / 1000);

    msgEl.appendChild(senderEl);
    msgEl.appendChild(bubbleEl);
    msgEl.appendChild(timeEl);
    messagesDiv.appendChild(msgEl);
    scrollToBottom();
}

/**
 * Add a system message to the message area.
 * @param {string} text - System message text.
 */
function addSystemMessage(text) {
    const el = document.createElement("div");
    el.className = "system-message";
    const span = document.createElement("span");
    span.textContent = text;
    el.appendChild(span);
    messagesDiv.appendChild(el);
    scrollToBottom();
}

// ═══════════════════════════════════════════════
// SENDING MESSAGES
// ═══════════════════════════════════════════════

/**
 * Send the current chat message.
 */
async function sendMessage() {
    const text = msgInput.value.trim();
    if (!text) return;

    let target = "all";
    let actualText = text;

    // Check for @username prefix for unicast
    const atMatch = text.match(/^@(\S+)\s+(.+)$/);
    if (atMatch) {
        target = atMatch[1];
        actualText = atMatch[2];
    }

    // Optimistic UI: show message immediately
    addChatMessage(currentUsername, actualText, Date.now() / 1000, true);
    msgInput.value = "";

    try {
        await apiCall("/api/send", "POST", { target, text: actualText });
    } catch (err) {
        addSystemMessage("⚠️ Gửi thất bại: " + err.message);
    }
}

btnSend.addEventListener("click", sendMessage);

msgInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// ═══════════════════════════════════════════════
// FILE TRANSFERS
// ═══════════════════════════════════════════════

btnFile.addEventListener("click", () => {
    fileDialog.classList.remove("hidden");
    filePathInput.value = "";
    fileTargetInput.value = "all";
    filePathInput.focus();
});

fileCancelBtn.addEventListener("click", () => {
    fileDialog.classList.add("hidden");
});

document.querySelector(".dialog-backdrop")?.addEventListener("click", () => {
    fileDialog.classList.add("hidden");
});

fileSendBtn.addEventListener("click", async () => {
    const filePath = filePathInput.value.trim();
    const target = fileTargetInput.value.trim() || "all";

    if (!filePath) return;

    fileDialog.classList.add("hidden");

    try {
        const result = await apiCall("/api/file", "POST", {
            file_path: filePath,
            target: target,
        });

        if (result.status === "ok") {
            addSystemMessage(`📤 Bắt đầu gửi file... (Stream #${result.stream_id})`);
        } else {
            addSystemMessage("⚠️ " + result.message);
        }
    } catch (err) {
        addSystemMessage("⚠️ Lỗi gửi file: " + err.message);
    }
});

/**
 * Add a file transfer item to the sidebar.
 * @param {number} streamId - Stream ID.
 * @param {string} filename - File name.
 * @param {number} size - File size in bytes.
 */
function addFileTransfer(streamId, filename, size) {
    // Remove empty state
    const emptyState = fileTransfersDiv.querySelector(".empty-state");
    if (emptyState) emptyState.remove();

    const item = document.createElement("div");
    item.className = "transfer-item";
    item.id = `transfer-${streamId}`;

    item.innerHTML = `
        <span class="transfer-name" title="${filename}">${filename}</span>
        <div class="progress-bar-bg">
            <div class="progress-bar-fill" style="width: 0%"></div>
        </div>
        <div class="transfer-status">${formatBytes(0)} / ${formatBytes(size)} — Đang truyền</div>
    `;

    fileTransfersDiv.appendChild(item);
}

/**
 * Update file transfer progress in the sidebar.
 * @param {number} streamId - Stream ID.
 * @param {string} status - Status string.
 */
function updateFileTransferStatus(streamId, status) {
    const item = document.getElementById(`transfer-${streamId}`);
    if (!item) return;

    const fill = item.querySelector(".progress-bar-fill");
    const statusEl = item.querySelector(".transfer-status");

    if (status === "completed") {
        fill.style.width = "100%";
        statusEl.textContent = "✅ Hoàn tất";
    } else if (status === "error") {
        fill.style.background = "var(--danger)";
        statusEl.textContent = "❌ Lỗi";
    }
}

// Poll file transfer progress periodically
setInterval(async () => {
    if (!isConnected) return;

    try {
        const progress = await apiCall("/api/progress", "GET");
        for (const [streamId, info] of Object.entries(progress)) {
            const item = document.getElementById(`transfer-${streamId}`);
            if (!item) continue;

            const fill = item.querySelector(".progress-bar-fill");
            const statusEl = item.querySelector(".transfer-status");
            const pct = info.size > 0 ? Math.min(100, (info.sent / info.size) * 100) : 0;

            fill.style.width = pct + "%";
            statusEl.textContent = `${formatBytes(info.sent)} / ${formatBytes(info.size)} — ${info.status}`;
        }
    } catch (err) {
        // Ignore polling errors
    }
}, 2000);

// ═══════════════════════════════════════════════
// DISCONNECT
// ═══════════════════════════════════════════════

btnDisconnect.addEventListener("click", () => {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    isConnected = false;
    chatScreen.classList.remove("active");
    loginScreen.classList.add("active");
    loginBtn.querySelector(".btn-text").textContent = "Đăng nhập";
    loginBtn.querySelector(".btn-spinner").classList.add("hidden");
    loginBtn.disabled = false;
    // Clear messages
    messagesDiv.innerHTML = `
        <div class="system-message">
            <span>Chào mừng đến StreamChat! Bạn đang ở phòng chat chung.</span>
        </div>
    `;
});
