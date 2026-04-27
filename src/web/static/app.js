(function() {
    'use strict';

    const API = '';
    let currentSessionId = null;
    let isStreaming = false;
    let activeTab = 'chat';

    const $ = s => document.querySelector(s);

    // Header
    const elName = $('#agentName'), elDot = $('#statusDot'), elStatus = $('#statusText');
    const elCalls = $('#statCalls'), elCost = $('#statCost'), elModel = $('#infoModel'), elTools = $('#infoTools');
    // Sidebar
    const elTasks = $('#infoTasks'), elTaskList = $('#tasksList');
    const elRing = $('#avatarRing'), elLabel = $('#avatarLabel');
    // Main
    const elPath = $('#mainPath');
    const wsBody = $('#workspaceBody'), chatBody = $('#chatBody'), logsBody = $('#logsBody');
    const wsStatus = $('#wsStatus'), wsMemory = $('#wsMemory'), wsPanel = $('#wsPanel'), wsSubagents = $('#wsSubagents');
    const chatMsgs = $('#chatMessages'), chatIn = $('#chatInput'), btnSend = $('#sendBtn');
    const logContent = $('#logContent');
    const elTodoList = $('#todoList');

    // ==================== INIT ====================
    document.addEventListener('DOMContentLoaded', () => {
        bind();
        fetchAll();
        setInterval(fetchAll, 4000);
        initResize();
    });

    function bind() {
        btnSend.addEventListener('click', sendMsg);
        chatIn.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
        });
        chatIn.addEventListener('input', () => {
            chatIn.style.height = 'auto';
            chatIn.style.height = Math.min(chatIn.scrollHeight, 100) + 'px';
        });
        document.querySelectorAll('.main-tab').forEach(t => {
            t.addEventListener('click', () => switchTab(t.dataset.tab));
        });
    }

    function switchTab(tab) {
        activeTab = tab;
        document.querySelectorAll('.main-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
        wsBody.style.display = tab === 'workspace' ? 'flex' : 'none';
        chatBody.style.display = tab === 'chat' ? 'flex' : 'none';
        logsBody.style.display = tab === 'logs' ? 'flex' : 'none';
        if (tab === 'chat') { chatIn.focus(); chatMsgs.scrollTop = chatMsgs.scrollHeight; }
    }
    // Init: chat is default
    switchTab('chat');

    // ==================== RESIZE ====================
    function initResize() {
        const sidebar = $('#sidebar');
        const handle = $('#resizeHandle');
        let dragging = false, startX, startW;

        handle.addEventListener('mousedown', e => {
            dragging = true;
            startX = e.clientX;
            startW = sidebar.offsetWidth;
            handle.classList.add('active');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });

        document.addEventListener('mousemove', e => {
            if (!dragging) return;
            const w = Math.max(200, Math.min(500, startW + e.clientX - startX));
            sidebar.style.width = w + 'px';
            sidebar.style.minWidth = w + 'px';
        });

        document.addEventListener('mouseup', () => {
            if (!dragging) return;
            dragging = false;
            handle.classList.remove('active');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }

    // ==================== AVATAR ====================
    function setAvatar(s) {
        elRing.className = 'avatar-ring ' + s;
        elLabel.textContent = s;
    }

    // ==================== FETCH ====================
    async function fetchAll() {
        await Promise.all([fetchStatus(), fetchPanelTasks(), fetchTodos()]);
    }

    async function fetchStatus() {
        try {
            const r = await fetch(API + '/api/agent/status');
            if (!r.ok) throw 0;
            const d = await r.json();
            elName.textContent = d.name || 'Agent';
            elDot.className = 'status-dot ' + (isStreaming ? 'running' : 'connected');
            elStatus.textContent = isStreaming ? 'processing' : d.status || 'ready';
            elModel.textContent = d.model || '-';
            elTools.textContent = (d.tools || []).length;
            if (d.usage) { elCalls.textContent = d.usage.total_calls||0; elCost.textContent = d.usage.total_cost_cny||'0'; }
            if (!isStreaming) setAvatar(d.status === 'running' ? 'thinking' : 'idle');
            wsStatus.innerHTML = 'Name: ' + esc(d.name||'-') +
                '\nModel: ' + esc(d.model||'-') +
                '\nStatus: ' + esc(d.status||'-') +
                '\nTools: ' + esc(String((d.tools||[]).length)) +
                '\nTasks: ' + esc(JSON.stringify(d.tasks||{}));
            const subs = d.subagents || [];
            if (!subs.length) {
                wsSubagents.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:12px;">No sub-agents</div>';
            } else {
                wsSubagents.innerHTML = subs.map(s =>
                    '<div class="subagent-card">' +
                    '<div class="subagent-name">&#128187; ' + esc(s.name) + '</div>' +
                    '<div class="subagent-desc">' + esc((s.description || 'No description').substring(0, 120)) + '</div>' +
                    '</div>'
                ).join('');
            }
            // Panel stats in workspace
            if (d.panel) {
                wsPanel.textContent = wsPanel.textContent ||
                    'Total: ' + d.panel.total + '  Pending: ' + d.panel.pending +
                    '  Active: ' + d.panel.active + '  Done: ' + d.panel.completed;
            }
        } catch { elDot.className = 'status-dot'; elStatus.textContent = 'disconnected'; }
    }

    async function fetchPanelTasks() {
        try {
            const r = await fetch(API + '/api/panel');
            if (!r.ok) {
                elTaskList.innerHTML = '<div class="empty-state">Panel HTTP ' + r.status + '</div>';
                wsPanel.textContent = 'Panel HTTP ' + r.status;
                return;
            }
            const d = await r.json();
            const tasks = d.tasks || [];
            const stats = d.stats || {};

            // Workspace card
            if (!tasks.length) {
                wsPanel.textContent = 'Empty (' + stats.total + ' total, ' + stats.pending + ' pending, ' + stats.completed + ' done)';
            } else {
                wsPanel.textContent = tasks.map(t =>
                    '[' + t.status + '] ' + t.title +
                    (t.interval ? ' (' + fmti(t.interval) + ')' : '') +
                    ' [' + t.source + ']'
                ).join('\n');
            }

            // Sidebar — show all tasks
            elTasks.textContent = tasks.length;
            if (!tasks.length) {
                elTaskList.innerHTML = '<div class="empty-state">Panel empty</div>';
            } else {
                elTaskList.innerHTML = tasks.map(t => {
                    let h = '<div class="task-card ' + (t.status === 'active' ? 'running' : t.status) + '">';
                    h += '<div class="task-title">' + esc(t.title) + '</div>';
                    h += '<div class="task-meta">';
                    h += '<span>' + t.status + '</span>';
                    h += '<span class="task-source">' + esc(t.source === 'llm' ? 'AI' : t.source === 'user' ? '你' : t.source) + '</span>';
                    if (t.interval) h += '<span class="task-interval">' + fmti(t.interval) + '</span>';
                    h += '<span>' + fmt(t.created_at) + '</span>';
                    h += '</div></div>';
                    return h;
                }).join('');
            }
        } catch (e) {
            elTaskList.innerHTML = '<div class="empty-state">Fetch failed: ' + esc(e.message) + '</div>';
            wsPanel.textContent = 'Fetch failed: ' + e.message;
        }
    }

    function renderTasks(tasks) {
        if (!tasks.length) { elTaskList.innerHTML = '<div class="empty-state">No tasks</div>'; return; }
        tasks.sort((a,b) => {
            const o = {running:0, pending:1, completed:2, failed:3};
            return (o[a.status]||4) - (o[b.status]||4);
        });
        elTaskList.innerHTML = tasks.map(t => {
            let h = '<div class="task-card ' + t.status + '">';
            h += '<div class="task-title">' + esc(t.title) + '</div>';
            h += '<div class="task-meta">';
            h += '<span>' + t.status + '</span>';
            if (t.source) h += '<span class="task-source">' + esc(t.source) + '</span>';
            if (t.interval) h += '<span class="task-interval">' + fmti(t.interval) + '</span>';
            h += '<span>' + esc(t.time) + '</span>';
            h += '</div></div>';
            return h;
        }).join('');
    }

    // ==================== TODOS ====================
    async function fetchTodos() {
        try {
            const r = await fetch(API + '/api/todos');
            if (!r.ok) return;
            const d = await r.json();
            const todos = d.todos || [];
            if (!todos.length) { elTodoList.innerHTML = '<div class="empty-state">No todos</div>'; return; }
            elTodoList.innerHTML = todos.map(t => {
                const sc = t.priority === 'high' ? 'var(--accent-red)' : t.priority === 'medium' ? 'var(--accent-orange)' : 'var(--text-muted)';
                const icon = t.status === 'completed' ? '&#10003;' : t.status === 'in_progress' ? '&#9654;' : t.status === 'cancelled' ? '&#10005;' : '&#9679;';
                const cls = t.status === 'completed' ? ' todo-done' : t.status === 'cancelled' ? ' todo-cancelled' : '';
                return '<div class="todo-item' + cls + '">' +
                    '<span class="todo-dot" style="color:' + sc + '">' + icon + '</span>' +
                    '<span class="todo-text">' + esc(t.content) + '</span>' +
                    '</div>';
            }).join('');
        } catch {}
    }

    // ==================== CHAT ====================
    function addMsg(role, content, streaming) {
        const w = chatMsgs.querySelector('.welcome-text'); if (w) w.remove();
        const el = document.createElement('div');
        el.className = 'msg ' + role;
        const rendered = role === 'user' ? esc(content) : md(content);
        el.innerHTML =
            '<div class="msg-avatar">' + (role === 'user' ? 'U' : 'A') + '</div>' +
            '<div class="msg-body">' +
            '<div class="msg-bubble"' + (streaming ? ' id="streamBubble"' : '') + '>' +
            (streaming ? '<div class="typing-indicator"><span></span><span></span><span></span></div>' : rendered) +
            '</div><div class="msg-time">' + new Date().toLocaleTimeString() + '</div></div>';
        chatMsgs.appendChild(el);
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
        return el.querySelector('.msg-bubble');
    }

    function streamToken(tok) {
        const b = document.getElementById('streamBubble'); if (!b) return;
        const ti = b.querySelector('.typing-indicator'); if (ti) ti.remove();
        b.textContent = (b.dataset.t||'') + tok; b.dataset.t = b.textContent;
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    function finishStream(text) {
        const b = document.getElementById('streamBubble'); if (!b) return;
        b.removeAttribute('id');
        b.innerHTML = md(text);
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    async function sendMsg() {
        const msg = chatIn.value.trim();
        if (!msg || isStreaming) return;
        chatIn.value = ''; chatIn.style.height = 'auto';
        btnSend.disabled = true; isStreaming = true;
        setAvatar('thinking');
        elDot.className = 'status-dot running';

        if (!currentSessionId) currentSessionId = 'web_' + Math.random().toString(36).slice(2,10);
        addMsg('user', msg);
        addMsg('assistant', '', true);

        try {
            const r = await fetch(API + '/api/chat/stream', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({message:msg, session_id:currentSessionId}),
            });
            const reader = r.body.getReader();
            const dec = new TextDecoder(); let buf = '';
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                buf += dec.decode(value, {stream:true});
                const lines = buf.split('\n'); buf = lines.pop()||'';
                for (const l of lines) {
                    if (!l.startsWith('data: ')) continue;
                    try {
                        const ev = JSON.parse(l.slice(6));
                        if (ev.type === 'token') { setAvatar('speaking'); streamToken(ev.content); }
                        else if (ev.type === 'done') finishStream(ev.content);
                        else if (ev.type === 'error') finishStream('Error: ' + ev.content);
                    } catch {}
                }
            }
        } catch (err) {
            finishStream('Error: ' + err.message);
        } finally {
            isStreaming = false; btnSend.disabled = false;
            setAvatar('idle');
            elDot.className = 'status-dot connected'; elStatus.textContent = 'ready';
            fetchAll(); chatIn.focus();
        }
    }

    // ==================== UTILS ====================
    function esc(s) { const d = document.createElement('div'); d.textContent = s||''; return d.innerHTML; }
    function fmt(ts) { if (!ts) return ''; return new Date(ts).toLocaleTimeString(); }
    function fmti(s) { return s >= 3600 ? (s/3600).toFixed(1)+'h' : s >= 60 ? (s/60).toFixed(0)+'m' : s+'s'; }

    function md(text) {
        if (!text) return '';
        let html = esc(text);
        // Code blocks first
        html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, function(_, lang, code) {
            return '<pre><code>' + esc(code) + '</code></pre>';
        });
        // Inline code
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        // Headers
        html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
        html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
        // Bold / Italic
        html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
        // Blockquote
        html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
        // Unordered list
        html = html.replace(/^[\-\*] (.+)$/gm, '<li>$1</li>');
        html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
        // Ordered list
        html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
        // Links
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
        // Horizontal rule
        html = html.replace(/^---$/gm, '<hr>');
        // Paragraphs: blank lines
        html = html.replace(/\n\n/g, '</p><p>');
        html = '<p>' + html + '</p>';
        // Clean empty paragraphs
        html = html.replace(/<p>\s*<\/p>/g, '');
        return html;
    }
})();
