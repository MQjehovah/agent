(function() {
    'use strict';

    const API_BASE = '';
    let currentSessionId = null;
    let isStreaming = false;
    let taskRefreshTimer = null;

    // ======================== DOM REFS ========================
    const $ = (sel) => document.querySelector(sel);
    const agentNameEl = $('#agentName');
    const statusDot = $('#statusDot');
    const statusText = $('#statusText');
    const statCalls = $('#statCalls');
    const statCost = $('#statCost');
    const infoModel = $('#infoModel');
    const infoTools = $('#infoTools');
    const infoTasks = $('#infoTasks');
    const avatarHead = $('.avatar-head');
    const avatarBadge = $('#avatarBadge');
    const badgeIcon = $('#badgeIcon');
    const badgeText = $('#badgeText');
    const chatMessages = $('#chatMessages');
    const chatInput = $('#chatInput');
    const sendBtn = $('#sendBtn');
    const refreshTasks = $('#refreshTasks');
    const clearChat = $('#clearChat');
    const particlesContainer = $('#particles');

    // ======================== INIT ========================
    document.addEventListener('DOMContentLoaded', () => {
        initParticles();
        bindEvents();
        fetchAgentStatus();
        fetchTasks();
        setInterval(fetchAgentStatus, 5000);
        setInterval(fetchTasks, 3000);
    });

    function bindEvents() {
        sendBtn.addEventListener('click', sendMessage);
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
        chatInput.addEventListener('input', autoResize);
        refreshTasks.addEventListener('click', fetchTasks);
        clearChat.addEventListener('click', clearChatHistory);
    }

    function autoResize() {
        chatInput.style.height = 'auto';
        chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
    }

    // ======================== PARTICLES ========================
    function initParticles() {
        for (let i = 0; i < 12; i++) {
            const p = document.createElement('div');
            p.className = 'particle';
            p.style.left = Math.random() * 100 + '%';
            p.style.top = 50 + Math.random() * 50 + '%';
            p.style.animationDelay = Math.random() * 3 + 's';
            p.style.animationDuration = 2 + Math.random() * 2 + 's';
            particlesContainer.appendChild(p);
        }
    }

    // ======================== AVATAR STATE ========================
    function setAvatarState(state) {
        avatarHead.className = 'avatar-head ' + state;
        avatarBadge.className = 'avatar-status-badge badge-' + state;
        const labels = {
            idle: ['\u25CF', 'idle'],
            thinking: ['\u26A1', 'thinking...'],
            speaking: ['\u25CF', 'speaking'],
            error: ['\u2716', 'error'],
        };
        const [icon, text] = labels[state] || labels.idle;
        badgeIcon.textContent = icon;
        badgeText.textContent = text;
    }

    // ======================== API CALLS ========================
    async function fetchAgentStatus() {
        try {
            const res = await fetch(API_BASE + '/api/agent/status');
            if (!res.ok) throw new Error('not ok');
            const data = await res.json();

            agentNameEl.textContent = data.name || 'Agent';
            statusDot.className = 'status-dot ' + (isStreaming ? 'running' : 'connected');
            statusText.textContent = data.status || 'ready';
            infoModel.textContent = data.model || '-';
            infoTools.textContent = (data.tools || []).length;
            infoTasks.textContent = Object.values(data.tasks || {}).reduce((a, b) => a + b, 0);

            if (data.usage) {
                statCalls.textContent = data.usage.total_calls || 0;
                statCost.textContent = (data.usage.total_cost_cny || '0');
            }

            if (!isStreaming) {
                if (data.status === 'running') setAvatarState('thinking');
                else if (data.status === 'completed') setAvatarState('idle');
                else setAvatarState('idle');
            }
        } catch {
            statusDot.className = 'status-dot';
            statusText.textContent = 'disconnected';
        }
    }

    async function fetchTasks() {
        try {
            const res = await fetch(API_BASE + '/api/tasks');
            if (!res.ok) return;
            const data = await res.json();
            renderKanban(data.tasks || []);
        } catch {}
    }

    // ======================== KANBAN ========================
    function renderKanban(tasks) {
        const groups = { pending: [], running: [], completed: [], failed: [], cancelled: [] };
        tasks.forEach(t => {
            const s = t.status || 'pending';
            if (groups[s]) groups[s].push(t);
            else groups.pending.push(t);
        });

        ['pending', 'running', 'completed', 'failed'].forEach(status => {
            const col = document.getElementById('col' + status.charAt(0).toUpperCase() + status.slice(1));
            const countEl = document.getElementById('count' + status.charAt(0).toUpperCase() + status.slice(1));
            const items = groups[status];
            countEl.textContent = items.length;

            if (items.length === 0) {
                col.innerHTML = '<div class="empty-state">No ' + status + ' tasks</div>';
                return;
            }

            col.innerHTML = items.map(t => renderTaskCard(t)).join('');
        });

        // Bind cancel buttons
        document.querySelectorAll('.btn-cancel').forEach(btn => {
            btn.addEventListener('click', () => cancelTask(btn.dataset.id));
        });
    }

    function renderTaskCard(task) {
        const time = task.created_at ? new Date(task.created_at).toLocaleTimeString() : '';
        let html = '<div class="task-card ' + task.status + '">';
        html += '<div class="task-id">#' + escapeHtml(task.id || '') + '</div>';
        html += '<div class="task-desc">' + escapeHtml(task.description || '') + '</div>';
        html += '<div class="task-time">' + escapeHtml(time) + '</div>';
        if (task.error) {
            html += '<div class="task-error">' + escapeHtml(task.error) + '</div>';
        }
        if (task.status === 'running') {
            html += '<div class="task-actions"><button class="btn-cancel" data-id="' + escapeHtml(task.id) + '">Cancel</button></div>';
        }
        html += '</div>';
        return html;
    }

    async function cancelTask(taskId) {
        try {
            await fetch(API_BASE + '/api/tasks/' + taskId + '/cancel', { method: 'POST' });
            fetchTasks();
        } catch {}
    }

    // ======================== CHAT ========================
    function addChatMessage(role, content, streaming) {
        const hasWelcome = chatMessages.querySelector('.welcome-message');
        if (hasWelcome) hasWelcome.remove();

        const msgEl = document.createElement('div');
        msgEl.className = 'msg ' + role;

        const avatarEl = document.createElement('div');
        avatarEl.className = 'msg-avatar';
        avatarEl.textContent = role === 'user' ? 'U' : 'A';

        const contentWrap = document.createElement('div');

        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble';

        if (streaming) {
            bubble.id = 'streamBubble';
            const indicator = document.createElement('div');
            indicator.className = 'typing-indicator';
            indicator.innerHTML = '<span></span><span></span><span></span>';
            bubble.appendChild(indicator);
        } else {
            bubble.textContent = content;
        }

        const timeEl = document.createElement('div');
        timeEl.className = 'msg-time';
        timeEl.textContent = new Date().toLocaleTimeString();

        contentWrap.appendChild(bubble);
        contentWrap.appendChild(timeEl);
        msgEl.appendChild(avatarEl);
        msgEl.appendChild(contentWrap);
        chatMessages.appendChild(msgEl);
        chatMessages.scrollTop = chatMessages.scrollHeight;

        return bubble;
    }

    function updateStreamBubble(token) {
        const bubble = document.getElementById('streamBubble');
        if (!bubble) return;
        const indicator = bubble.querySelector('.typing-indicator');
        if (indicator) indicator.remove();

        if (!bubble.dataset.text) bubble.dataset.text = '';
        bubble.dataset.text += token;
        bubble.textContent = bubble.dataset.text;
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function finalizeStreamBubble(fullContent) {
        const bubble = document.getElementById('streamBubble');
        if (!bubble) return;
        bubble.removeAttribute('id');
        bubble.textContent = fullContent;
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    async function sendMessage() {
        const message = chatInput.value.trim();
        if (!message || isStreaming) return;

        chatInput.value = '';
        chatInput.style.height = 'auto';
        sendBtn.disabled = true;
        isStreaming = true;
        setAvatarState('thinking');
        statusDot.className = 'status-dot running';

        if (!currentSessionId) {
            currentSessionId = 'web_' + Math.random().toString(36).substring(2, 10);
        }

        addChatMessage('user', message);
        addChatMessage('assistant', '', true);

        try {
            const res = await fetch(API_BASE + '/api/chat/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message, session_id: currentSessionId }),
            });

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    try {
                        const event = JSON.parse(line.slice(6));
                        if (event.type === 'token') {
                            setAvatarState('speaking');
                            updateStreamBubble(event.content);
                        } else if (event.type === 'done') {
                            finalizeStreamBubble(event.content);
                        } else if (event.type === 'error') {
                            finalizeStreamBubble('Error: ' + event.content);
                        }
                    } catch {}
                }
            }
        } catch (err) {
            finalizeStreamBubble('Connection error: ' + err.message);
        } finally {
            isStreaming = false;
            sendBtn.disabled = false;
            setAvatarState('idle');
            statusDot.className = 'status-dot connected';
            fetchAgentStatus();
            fetchTasks();
            chatInput.focus();
        }
    }

    function clearChatHistory() {
        chatMessages.innerHTML =
            '<div class="welcome-message">' +
                '<div class="welcome-icon">\uD83E\uDD16</div>' +
                '<p>Hello! I\'m your AI Agent. How can I help you today?</p>' +
            '</div>';
        currentSessionId = null;
    }

    // ======================== UTILS ========================
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
})();
