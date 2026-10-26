/**
 * StreamChat — Client-side Application Logic
 *
 * Sử dụng kiến trúc Client-Server chuẩn với HTTP REST và Server-Sent Events (SSE).
 * Tuân thủ 100% quy định môn học: Không sử dụng thư viện WebSocket.
 * Quản lý độc lập từng phiên trình duyệt trên cùng một địa chỉ:cổng 8080.
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
const chatTitle = document.getElementById("chat-title");
const navAll = document.getElementById("nav-all");
const onlineUsersDiv = document.getElementById("online-users");
const onlineCountSpan = document.getElementById("online-count");

// File Dialog
const fileDialog = document.getElementById("file-dialog");
const fileDialogClose = document.getElementById("file-dialog-close");
const fileInput = document.getElementById("file-input");
const fileDropZone = document.getElementById("file-drop-zone");
const btnBrowseFile = document.getElementById("btn-browse-file");
const filePreviewCard = document.getElementById("file-preview-card");
const filePreviewName = document.getElementById("file-preview-name");
const filePreviewSize = document.getElementById("file-preview-size");
const fileRemoveBtn = document.getElementById("file-remove-btn");
const fileTargetInput = document.getElementById("file-target-input");
const toggleManualPath = document.getElementById("toggle-manual-path");
const manualPathContainer = document.getElementById("manual-path-container");
const filePathInput = document.getElementById("file-path-input");
const fileSendBtn = document.getElementById("file-send-btn");
const fileCancelBtn = document.getElementById("file-cancel-btn");
const fileTransfersDiv = document.getElementById("file-transfers");

// ═══════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════

let currentUsername = "";
let isConnected = false;
let activeTarget = "all";  // "all" hoặc username cụ thể
let eventSource = null;
let selectedFile = null;

// ═══════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════

function formatTime(ts) {
    const d = ts ? new Date(ts * 1000) : new Date();
    const h = d.getHours().toString().padStart(2, "0");
    const m = d.getMinutes().toString().padStart(2, "0");
    return `${h}:${m}`;
}

function formatBytes(bytes) {
    if (!bytes || bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(1) + " " + units[i];
}

function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
    });
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

// ═══════════════════════════════════════════════
// SERVER-SENT EVENTS (SSE) MANAGEMENT
// ═══════════════════════════════════════════════

/**
 * Khởi tạo kết nối SSE (EventSource) thời gian thực đến máy chủ.
 * @param {string} username - Tên người dùng đã đăng nhập.
 */
function initSSE(username) {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }

    const sseUrl = `/api/events?username=${encodeURIComponent(username)}`;
    try {
        eventSource = new EventSource(sseUrl);
    } catch (err) {
        showLoginError("Trình duyệt không hỗ trợ EventSource: " + err.message);
        return;
    }

    eventSource.onopen = () => {
        connectionStatus.textContent = "● Đã kết nối (HTTP/SSE)";
        connectionStatus.className = "conn-status connected";
    };

    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleServerEvent(data);
        } catch (err) {
            console.warn("SSE parse error:", err, event.data);
        }
    };

    eventSource.onerror = () => {
        if (isConnected) {
            connectionStatus.textContent = "● Đang kết nối lại...";
            connectionStatus.className = "conn-status disconnected";
        }
    };
}

// ═══════════════════════════════════════════════
// LOGIN FLOW (HTTP POST)
// ═══════════════════════════════════════════════

loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = loginUsername.value.trim().toLowerCase();
    const password = loginPassword.value.trim();

    if (!username || !password) return;

    loginBtn.querySelector(".btn-text").textContent = "Đang xác thực...";
    loginBtn.querySelector(".btn-spinner").classList.remove("hidden");
    loginBtn.disabled = true;
    loginError.classList.add("hidden");

    try {
        const response = await fetch("/api/login", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                username: username,
                password: password,
            }),
        });

        const data = await response.json();

        if (response.ok && data.status === "ok") {
            currentUsername = data.username || username;
            initSSE(currentUsername);
            handleAuthSuccess(data);
        } else {
            showLoginError(data.message || "Tên đăng nhập hoặc mật khẩu không chính xác.");
        }
    } catch (err) {
        showLoginError("Lỗi kết nối máy chủ: " + err.message);
    }
});

function showLoginError(msg) {
    loginError.textContent = msg;
    loginError.classList.remove("hidden");
    loginBtn.querySelector(".btn-text").textContent = "Đăng nhập";
    loginBtn.querySelector(".btn-spinner").classList.add("hidden");
    loginBtn.disabled = false;
}

// ═══════════════════════════════════════════════
// SERVER EVENT DISPATCHER
// ═══════════════════════════════════════════════

function handleServerEvent(data) {
    const type = data.type;

    switch (type) {
        case "AUTH_RESP":
            handleAuthResponse(data);
            break;
        case "MSG":
            handleChatMessage(data);
            break;
        case "USER_LIST":
            handleUserList(data);
            break;
        case "SYSTEM":
            addSystemMessage(data.message);
            break;
        case "FILE_READY":
            handleFileReady(data);
            break;
        case "KICKED":
            handleKicked(data);
            break;
        case "ERROR":
            addSystemMessage("⚠️ " + data.message);
            break;
        default:
            console.log("Server event:", type, data);
    }
}

function handleKicked(data) {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    isConnected = false;
    currentUsername = "";
    activeTarget = "all";

    chatScreen.classList.remove("active");
    loginScreen.classList.add("active");

    showLoginError(data.message || "Tài khoản của bạn đã được đăng nhập từ một thiết bị hoặc tab khác.");

    messagesDiv.innerHTML = `
        <div class="system-message">
            <span>Chào mừng đến StreamChat! Bạn đang ở phòng chat chung.</span>
        </div>
    `;
    onlineUsersDiv.innerHTML = '<p class="empty-state">Chưa có ai khác online</p>';
    onlineCountSpan.textContent = "0";
}

function handleAuthSuccess(data) {
    currentUsername = data.username || currentUsername;
    displayUsername.textContent = currentUsername;
    userAvatar.textContent = currentUsername.charAt(0).toUpperCase();

    loginScreen.classList.remove("active");
    chatScreen.classList.add("active");
    isConnected = true;
    msgInput.focus();

    addSystemMessage(data.message || `Đăng nhập thành công với tài khoản @${currentUsername}!`);
}

function handleAuthResponse(data) {
    if (data.success) {
        handleAuthSuccess(data);
    } else {
        showLoginError(data.message || "Tên đăng nhập hoặc mật khẩu không chính xác.");
        isConnected = false;
    }
}

function handleUserList(data) {
    const users = data.users || [];
    onlineCountSpan.textContent = users.length;

    // Lọc bỏ chính mình trong danh sách nhắn tin trực tiếp
    const otherUsers = users.filter((u) => u !== currentUsername);

    onlineUsersDiv.innerHTML = "";
    if (otherUsers.length === 0) {
        onlineUsersDiv.innerHTML = '<p class="empty-state">Chưa có ai khác online</p>';
        return;
    }

    otherUsers.forEach((username) => {
        const btn = document.createElement("button");
        btn.className = `user-item ${activeTarget === username ? "active" : ""}`;
        btn.dataset.username = username;
        btn.innerHTML = `
            <div class="user-item-avatar">${username.charAt(0).toUpperCase()}</div>
            <div class="user-item-info">
                <span class="user-item-name">@${escapeHtml(username)}</span>
                <span class="user-item-status">● Đang online</span>
            </div>
        `;
        btn.addEventListener("click", () => {
            selectChatTarget(username);
        });
        onlineUsersDiv.appendChild(btn);
    });
}

/**
 * Chọn người nhận chat (Phòng chung hoặc nhắn riêng 1-1).
 */
function selectChatTarget(target) {
    activeTarget = target;

    // Cập nhật trạng thái active trên sidebar
    if (target === "all") {
        navAll.classList.add("active");
        chatTitle.textContent = "💬 Phòng chung";
        msgInput.placeholder = "Nhập tin nhắn... (@username để nhắn riêng)";
        if (fileTargetInput) fileTargetInput.value = "all";
    } else {
        navAll.classList.remove("active");
        chatTitle.textContent = `🔒 Chat riêng với @${target}`;
        msgInput.placeholder = `Nhắn tin riêng cho @${target}...`;
        if (fileTargetInput) fileTargetInput.value = target;
    }

    document.querySelectorAll(".user-item").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.username === target);
    });

    msgInput.focus();
}

navAll.addEventListener("click", () => {
    selectChatTarget("all");
});

// ═══════════════════════════════════════════════
// CHAT MESSAGES
// ═══════════════════════════════════════════════

function handleChatMessage(data) {
    const isOutgoing = data.from === currentUsername;
    addChatMessage(data.from, data.text, data.timestamp, isOutgoing, data.is_private, data.target);
}

function addChatMessage(sender, text, timestamp, isOutgoing, isPrivate = false, target = "all") {
    const msgEl = document.createElement("div");
    msgEl.className = `message ${isOutgoing ? "outgoing" : "incoming"} ${isPrivate ? "private-msg" : ""}`;

    const senderEl = document.createElement("div");
    senderEl.className = "message-sender";
    if (isPrivate) {
        if (isOutgoing) {
            senderEl.innerHTML = `Bạn ➔ @${escapeHtml(target)} <span class="private-badge">🔒 Riêng tư</span>`;
        } else {
            senderEl.innerHTML = `@${escapeHtml(sender)} <span class="private-badge">🔒 Riêng tư</span>`;
        }
    } else {
        senderEl.textContent = isOutgoing ? "Bạn" : `@${sender}`;
    }

    const bubbleEl = document.createElement("div");
    bubbleEl.className = "message-bubble";
    bubbleEl.textContent = text;

    const timeEl = document.createElement("div");
    timeEl.className = "message-time";
    timeEl.textContent = formatTime(timestamp);

    msgEl.appendChild(senderEl);
    msgEl.appendChild(bubbleEl);
    msgEl.appendChild(timeEl);
    messagesDiv.appendChild(msgEl);
    scrollToBottom();
}

function addSystemMessage(text) {
    const el = document.createElement("div");
    el.className = "system-message";
    const span = document.createElement("span");
    span.textContent = text;
    el.appendChild(span);
    messagesDiv.appendChild(el);
    scrollToBottom();
}

async function sendMessage() {
    const text = msgInput.value.trim();
    if (!text) return;

    let target = activeTarget;
    let actualText = text;

    // Kiểm tra tiền tố @username nếu nhập tay
    const atMatch = text.match(/^@(\S+)\s+(.+)$/);
    if (atMatch) {
        target = atMatch[1].toLowerCase();
        actualText = atMatch[2];
    }

    msgInput.value = "";

    try {
        const res = await fetch("/api/send", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                from: currentUsername,
                target: target,
                text: actualText,
            }),
        });

        const result = await res.json();
        if (!res.ok || result.status !== "ok") {
            addSystemMessage("⚠️ Không thể gửi tin nhắn: " + (result.message || "Lỗi máy chủ"));
        }
    } catch (err) {
        addSystemMessage("⚠️ Lỗi mạng khi gửi tin nhắn: " + err.message);
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
// FILE TRANSFER
// ═══════════════════════════════════════════════

function handleFileReady(data) {
    const isOutgoing = data.from === currentUsername;
    const isTarget = data.target === "all" || data.target === currentUsername || isOutgoing;
    if (!isTarget) return;

    // 1. Thêm thẻ tệp tin vào cửa sổ chat
    addFileMessage(data.from, data.filename, data.size, data.download_url, isOutgoing, data.target);

    // 2. Thêm vào danh sách tệp bên sidebar
    addFileTransferSidebar(data.stream_id, data.filename, data.size, data.download_url);
}

function addFileMessage(sender, filename, size, downloadUrl, isOutgoing, target) {
    const msgEl = document.createElement("div");
    msgEl.className = `message ${isOutgoing ? "outgoing" : "incoming"}`;

    const senderEl = document.createElement("div");
    senderEl.className = "message-sender";
    const targetText = target !== "all" ? ` (Gửi riêng: @${target})` : "";
    senderEl.textContent = isOutgoing ? `Bạn đã gửi tệp${targetText}:` : `@${sender} đã gửi tệp${targetText}:`;

    const bubbleEl = document.createElement("div");
    bubbleEl.className = "message-bubble";

    const card = document.createElement("div");
    card.className = "file-card-msg";
    card.innerHTML = `
        <div class="file-card-icon">📁</div>
        <div class="file-card-details">
            <span class="file-card-name" title="${escapeHtml(filename)}">${escapeHtml(filename)}</span>
            <span class="file-card-size">${formatBytes(size)}</span>
        </div>
        <a href="${downloadUrl}" class="btn-download-file" target="_blank" download="${escapeHtml(filename)}">
            📥 Tải về
        </a>
    `;

    bubbleEl.appendChild(card);

    const timeEl = document.createElement("div");
    timeEl.className = "message-time";
    timeEl.textContent = formatTime();

    msgEl.appendChild(senderEl);
    msgEl.appendChild(bubbleEl);
    msgEl.appendChild(timeEl);
    messagesDiv.appendChild(msgEl);
    scrollToBottom();
}

function addFileTransferSidebar(streamId, filename, size, downloadUrl) {
    const emptyState = fileTransfersDiv.querySelector(".empty-state");
    if (emptyState) emptyState.remove();

    let item = document.getElementById(`transfer-${streamId}`);
    if (!item) {
        item = document.createElement("div");
        item.className = "transfer-item";
        item.id = `transfer-${streamId}`;
        fileTransfersDiv.prepend(item);
    }

    item.innerHTML = `
        <span class="transfer-name" title="${escapeHtml(filename)}">${escapeHtml(filename)}</span>
        <div class="progress-bar-bg">
            <div class="progress-bar-fill" style="width: 100%"></div>
        </div>
        <div class="transfer-status" style="display:flex; justify-content:space-between; align-items:center;">
            <span>${formatBytes(size)} — ✅ Hoàn tất</span>
            <a href="${downloadUrl}" style="color:var(--accent-primary); font-weight:600; text-decoration:none;" download>Tải</a>
        </div>
    `;
}

function updateFileSendBtnState() {
    const hasFile = selectedFile !== null;
    fileSendBtn.disabled = !hasFile;
}

function setSelectedFile(file) {
    if (!file) return;
    selectedFile = file;
    filePreviewName.textContent = file.name;
    filePreviewName.title = file.name;
    filePreviewSize.textContent = formatBytes(file.size);

    fileDropZone.classList.add("hidden");
    filePreviewCard.classList.remove("hidden");
    updateFileSendBtnState();
}

function clearSelectedFile() {
    selectedFile = null;
    if (fileInput) fileInput.value = "";
    filePreviewCard.classList.add("hidden");
    fileDropZone.classList.remove("hidden");
    updateFileSendBtnState();
}

function openFileDialog() {
    fileDialog.classList.remove("hidden");
    clearSelectedFile();
    if (fileTargetInput) fileTargetInput.value = activeTarget;
    fileSendBtn.textContent = "Gửi Tệp";
    updateFileSendBtnState();
}

function closeFileDialog() {
    fileDialog.classList.add("hidden");
    clearSelectedFile();
    fileSendBtn.textContent = "Gửi Tệp";
    updateFileSendBtnState();
}

btnFile.addEventListener("click", openFileDialog);
fileCancelBtn.addEventListener("click", closeFileDialog);
fileDialogClose?.addEventListener("click", closeFileDialog);
document.querySelector(".dialog-backdrop")?.addEventListener("click", closeFileDialog);

fileDropZone.addEventListener("click", () => {
    fileInput.click();
});

btnBrowseFile?.addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
});

fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files.length > 0) {
        setSelectedFile(fileInput.files[0]);
    }
});

fileRemoveBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    clearSelectedFile();
});

// Kéo thả file
fileDropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.stopPropagation();
    fileDropZone.classList.add("dragover");
});

fileDropZone.addEventListener("dragleave", (e) => {
    e.preventDefault();
    e.stopPropagation();
    fileDropZone.classList.remove("dragover");
});

fileDropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    fileDropZone.classList.remove("dragover");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        setSelectedFile(e.dataTransfer.files[0]);
    }
});

// Gửi file nhị phân lên server với thanh tiến trình và thông báo trực quan
fileSendBtn.addEventListener("click", () => {
    if (!selectedFile) return;

    const target = fileTargetInput.value.trim().toLowerCase() || "all";
    const fileToSend = selectedFile;
    fileSendBtn.disabled = true;
    const originalText = "Gửi Tệp";
    fileSendBtn.textContent = "Đang gửi... 0%";

    const url = `/api/file?target=${encodeURIComponent(target)}&filename=${encodeURIComponent(fileToSend.name)}&sender=${encodeURIComponent(currentUsername)}`;

    const xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(fileToSend.name));
    xhr.setRequestHeader("X-Target", encodeURIComponent(target));
    xhr.setRequestHeader("X-Sender", encodeURIComponent(currentUsername));

    // Bắt tiến trình upload để hiển thị % trực tiếp lên nút
    xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && e.total > 0) {
            const percent = Math.min(100, Math.round((e.loaded / e.total) * 100));
            fileSendBtn.textContent = `Đang gửi... ${percent}%`;
        }
    };

    xhr.onload = () => {
        try {
            const result = JSON.parse(xhr.responseText || "{}");
            if (xhr.status >= 200 && xhr.status < 300 && result.status === "ok") {
                fileSendBtn.textContent = "✅ Đã tải lên thành công!";
                addSystemMessage(`📤 Đã gửi thành công tệp tin ${fileToSend.name}!`);
                setTimeout(() => {
                    closeFileDialog();
                }, 1200);
            } else {
                addSystemMessage("⚠️ Không thể gửi file: " + (result.message || "Lỗi máy chủ"));
                fileSendBtn.disabled = false;
                fileSendBtn.textContent = originalText;
            }
        } catch (err) {
            addSystemMessage("⚠️ Lỗi phản hồi upload: " + err.message);
            fileSendBtn.disabled = false;
            fileSendBtn.textContent = originalText;
        }
    };

    xhr.onerror = () => {
        addSystemMessage("⚠️ Lỗi kết nối mạng khi upload file.");
        fileSendBtn.disabled = false;
        fileSendBtn.textContent = originalText;
    };

    xhr.send(fileToSend);
});

// ═══════════════════════════════════════════════
// DISCONNECT
// ═══════════════════════════════════════════════

btnDisconnect.addEventListener("click", () => {
    if (currentUsername) {
        fetch(`/api/logout?username=${encodeURIComponent(currentUsername)}`, { method: "POST" }).catch(() => {});
    }
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    isConnected = false;
    currentUsername = "";
    activeTarget = "all";

    chatScreen.classList.remove("active");
    loginScreen.classList.add("active");
    loginBtn.querySelector(".btn-text").textContent = "Đăng nhập";
    loginBtn.querySelector(".btn-spinner").classList.add("hidden");
    loginBtn.disabled = false;

    messagesDiv.innerHTML = `
        <div class="system-message">
            <span>Chào mừng đến StreamChat! Bạn đang ở phòng chat chung.</span>
        </div>
    `;
    onlineUsersDiv.innerHTML = '<p class="empty-state">Chưa có ai khác online</p>';
    onlineCountSpan.textContent = "0";
});
