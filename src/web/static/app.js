(function() {
    'use strict';

    const API = '';
    let currentSessionId = null;
    let isStreaming = false;
    let activeTab = 'chat';
    currentSessionId = sessionStorage.getItem('agent_session_id') || null;

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
    const sessionsBody = $('#sessionsBody');
    const wsStatus = $('#wsStatus'), wsMemory = $('#wsMemory'), wsPanel = $('#wsPanel'), wsSubagents = $('#wsSubagents');
    const chatMsgs = $('#chatMessages'), chatIn = $('#chatInput'), btnSend = $('#sendBtn');
    const btnNewChat = $('#newChatBtn');
    const logContent = $('#logContent');
    const elTodoList = $('#todoList');
    // Sessions
    const sessionsSessionList = $('#sessionsSessionList');
    const sessionsMessages = $('#sessionsMessages');
    const sessionMessagesList = $('#sessionMessagesList');
    const sessionMessagesHeader = $('#sessionMessagesHeader');
    const sessionsTotalCount = $('#sessionsTotalCount');
    const sessionsEmpty = $('#sessionsEmpty');

    // ==================== INIT ====================
    document.addEventListener('DOMContentLoaded', () => {
        bind();
        fetchAll();
        loadChatHistory();
        setInterval(fetchAll, 4000);
        initResize();
    });

    function bind() {
        btnSend.addEventListener('click', sendMsg);
        btnNewChat.addEventListener('click', newChat);
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
        sessionsBody.style.display = tab === 'sessions' ? 'flex' : 'none';
        logsBody.style.display = tab === 'logs' ? 'flex' : 'none';
        if (tab === 'chat') { chatIn.focus(); chatMsgs.scrollTop = chatMsgs.scrollHeight; }
        if (tab === 'sessions') { fetchAgentSessions(); }
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
    async function loadChatHistory() {
        if (!currentSessionId) return;
        try {
            const r = await fetch(API + '/api/sessions/' + currentSessionId + '/messages');
            if (!r.ok) return;
            const d = await r.json();
            if (!d.messages || !d.messages.length) return;
            const w = chatMsgs.querySelector('.welcome-text'); if (w) w.remove();
            d.messages.forEach(m => {
                if (m.role === 'system') return;
                const el = document.createElement('div');
                el.className = 'msg ' + (m.role === 'user' ? 'user' : 'assistant');
                el.innerHTML = '<div class="msg-avatar">' + (m.role === 'user' ? 'U' : 'A') + '</div>' +
                    '<div class="msg-body"><div class="msg-bubble">' + md(m.content || '') + '</div></div>';
                chatMsgs.appendChild(el);
            });
            chatMsgs.scrollTop = chatMsgs.scrollHeight;
        } catch {}
    }
    async function fetchTodos() {
        try {
            const r = await fetch(API + '/api/todos?status=active');
            if (!r.ok) return;
            const d = await r.json();
            const todos = d.todos || [];
            if (!todos.length) { elTodoList.innerHTML = '<div class="empty-state">No active todos</div>'; return; }

            const grouped = {};
            todos.forEach(t => {
                const aid = t.agent_id || 'main';
                grouped[aid] = grouped[aid] || [];
                grouped[aid].push(t);
            });

            let html = '';
            var sortedKeys = Object.keys(grouped).sort(function(a, b) {
                if (a === 'main') return 1;
                if (b === 'main') return -1;
                return 0;
            });
            sortedKeys.forEach(function(aid) {
                var isSubagent = aid !== 'main';
                if (Object.keys(grouped).length > 1) {
                    const active = grouped[aid].filter(t => t.status !== 'completed' && t.status !== 'cancelled').length;
                    var groupCls = isSubagent ? 'todo-group-label todo-group-subagent' : 'todo-group-label';
                    html += '<div class="' + groupCls + '">' + esc(aid) + ' (' + active + ')</div>';
                }
                grouped[aid].forEach(t => {
                    const st = t.status || 'pending';
                    const isDone = st === 'completed' || st === 'cancelled';
                    const stLabels = {
                        pending: { icon: '&#9679;', label: '待处理', color: 'var(--accent-orange)' },
                        in_progress: { icon: '&#9654;', label: '进行中', color: 'var(--accent-blue)' },
                        completed: { icon: '&#10003;', label: '已完成', color: 'var(--accent-green)' },
                        cancelled: { icon: '&#10005;', label: '已取消', color: 'var(--text-muted)' },
                    };
                    const si = stLabels[st] || stLabels.pending;
                    const cls = isDone ? ' todo-done' : '';
                    html += '<div class="todo-item' + cls + '">' +
                        '<span class="todo-dot" style="color:' + si.color + '">' + si.icon + '</span>' +
                        '<span class="todo-text">' + esc(t.content) + '</span>' +
                        '<span class="todo-status" style="color:' + si.color + '">' + si.label + '</span>' +
                        '</div>';
                });
            });
            elTodoList.innerHTML = html;
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
        b.dataset.t = (b.dataset.t||'') + tok;
        b.innerHTML = md(b.dataset.t);
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    function finishStream(text) {
        const b = document.getElementById('streamBubble'); if (!b) return;
        b.removeAttribute('id');
        var toolEvents = b.querySelectorAll('.tool-inline-event');
        for (var i = 0; i < toolEvents.length; i++) toolEvents[i].remove();
        var finalText = (text || '').trim();
        if (finalText) {
            b.innerHTML = md(finalText);
        } else {
            var msgEl = b.closest('.msg');
            if (msgEl) msgEl.remove();
        }
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    function startNewMainRound() {
        var b = document.getElementById('streamBubble');
        if (b) {
            var text = (b.dataset.t || '').trim();
            b.removeAttribute('id');
            var toolEvents = b.querySelectorAll('.tool-inline-event');
            for (var i = 0; i < toolEvents.length; i++) toolEvents[i].remove();
            if (text) {
                b.innerHTML = md(text);
            } else {
                var msgEl = b.closest('.msg');
                if (msgEl) msgEl.remove();
            }
        }
        addMsg('assistant', '', true);
    }

    // ---- Subagent streaming bubble management ----
    var _subagentBubbles = {}; // agent_name -> {bubble, accumulated}

    function _createSubagentBubble(agentName) {
        var w = chatMsgs.querySelector('.welcome-text'); if (w) w.remove();
        var el = document.createElement('div');
        el.className = 'msg subagent-msg';
        var avatarLetter = agentName ? agentName.charAt(0) : 'S';
        el.innerHTML =
            '<div class="msg-avatar subagent-avatar">' + esc(avatarLetter) + '</div>' +
            '<div class="msg-body">' +
            '<div class="msg-sender">' + esc(agentName) + '</div>' +
            '<div class="msg-bubble">' +
            '<div class="typing-indicator"><span></span><span></span><span></span></div>' +
            '</div><div class="msg-time">' + new Date().toLocaleTimeString() + '</div></div>';
        chatMsgs.appendChild(el);
        var bubble = el.querySelector('.msg-bubble');
        _subagentBubbles[agentName] = { bubble: bubble, accumulated: '' };
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
        return bubble;
    }

    function getOrCreateSubagentBubble(agentName, agentType) {
        if (_subagentBubbles[agentName]) return _subagentBubbles[agentName].bubble;
        return _createSubagentBubble(agentName);
    }

    function startNewSubagentRound(agentName) {
        // Finalize existing bubble if any, then create a fresh one
        if (_subagentBubbles[agentName]) {
            finalizeSubagentBubble(agentName, null);
        }
        _createSubagentBubble(agentName);
    }

    function streamSubagentToken(agentName, tok) {
        var info = _subagentBubbles[agentName];
        if (!info) return;
        var b = info.bubble;
        var ti = b.querySelector('.typing-indicator'); if (ti) ti.remove();
        info.accumulated += tok;
        b.innerHTML = md(info.accumulated);
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    function finalizeSubagentBubble(agentName, content) {
        var info = _subagentBubbles[agentName];
        if (!info) return;
        var b = info.bubble;
        b.removeAttribute('id');
        var toolEvents = b.querySelectorAll('.tool-inline-event');
        for (var i = 0; i < toolEvents.length; i++) toolEvents[i].remove();
        var text = content || info.accumulated || '';
        if (text) {
            b.innerHTML = md(text);
        } else {
            var msgEl = b.closest('.msg');
            if (msgEl) msgEl.remove();
        }
        delete _subagentBubbles[agentName];
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    function addSubagentToolEvent(type, data) {
        var agentName = data.agent_name || '';
        var bubble = _subagentBubbles[agentName];
        if (!bubble) {
            bubble = getOrCreateSubagentBubble(agentName, data.agent_type || 'subagent');
        }
        bubble = _subagentBubbles[agentName] ? _subagentBubbles[agentName].bubble : bubble;
        if (!bubble) return;
        var ti = bubble.querySelector('.typing-indicator'); if (ti) ti.remove();
        var el = document.createElement('div');
        el.className = 'tool-inline-event subagent-tool-inline';
        el.setAttribute('data-tool-name', data.name || '');

        if (type === 'subagent_tool_start') {
            el.className = 'tool-inline-event subagent-tool-inline tool-inline-running';
            el.innerHTML =
                '<span class="tool-spinner"></span> &#35843;&#29992;&#24037;&#20855;: <strong>' + esc(data.name) + '</strong>' +
                '<pre class="tool-inline-args">' + esc(JSON.stringify(data.args || {}, null, 2).substring(0, 200)) + '</pre>';
            // todowrite tool changes — refresh todos immediately
            if (data.name === 'todowrite') fetchTodos();
        } else if (type === 'subagent_tool_result') {
            var running = bubble.querySelector('.subagent-tool-inline.tool-inline-running[data-tool-name="' + CSS.escape(data.name || '') + '"]');
            if (running) {
                var s = running.querySelector('.tool-spinner');
                if (s) s.outerHTML = '&#10003;';
                running.className = 'tool-inline-event subagent-tool-inline tool-inline-done';
                var summary = (data.result || '').substring(0, 150);
                var summaryEl = document.createElement('span');
                summaryEl.className = 'tool-inline-summary';
                summaryEl.textContent = summary;
                running.appendChild(summaryEl);
            }
            chatMsgs.scrollTop = chatMsgs.scrollHeight;
            return;
        } else {
            return;
        }

        bubble.appendChild(el);
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    function addToolEvent(type, data) {
        // Subagent internal tool events go into subagent bubble
        if (type === 'subagent_tool_start' || type === 'subagent_tool_result') {
            addSubagentToolEvent(type, data);
            return;
        }

        var b = document.getElementById('streamBubble');
        if (!b) return;
        var ti = b.querySelector('.typing-indicator');
        if (ti) ti.remove();
        var el = document.createElement('div');
        el.className = 'tool-inline-event';
        el.setAttribute('data-tool-name', data.name || '');

        if (type === 'tool_start') {
            var argsStr = JSON.stringify(data.args || {}, null, 2);
            var argsShort = argsStr.length > 200 ? argsStr.substring(0, 200) + '...' : argsStr;
            el.className = 'tool-inline-event tool-inline-running';
            el.innerHTML =
                '<span class="tool-spinner"></span> &#35843;&#29992;&#24037;&#20855;: <strong>' + esc(data.name) + '</strong>' +
                '<pre class="tool-inline-args">' + esc(argsShort) + '</pre>';
            if (data.name === 'todowrite') fetchTodos();
        } else if (type === 'tool_result') {
            var running = b.querySelector('.tool-inline-running[data-tool-name="' + CSS.escape(data.name || '') + '"]');
            if (running) {
                running.querySelector('.tool-spinner').outerHTML = '&#10003;';
                running.className = 'tool-inline-event tool-inline-done';
                var summary = (data.result || '').substring(0, 150);
                var summaryEl = document.createElement('span');
                summaryEl.className = 'tool-inline-summary';
                summaryEl.textContent = summary;
                running.appendChild(summaryEl);
            }
            chatMsgs.scrollTop = chatMsgs.scrollHeight;
            return;
        } else if (type === 'subagent_start') {
            el.className = 'tool-inline-event subagent-inline tool-inline-running';
            el.innerHTML =
                '<span class="tool-spinner subagent-spinner"></span> &#129302; &#23376;&#20195;&#29702;: <strong>' + esc(data.name) + '</strong> &#27491;&#22312;&#25191;&#34892;...' +
                '<div class="tool-inline-task">' + esc(data.task || '') + '</div>';
        } else if (type === 'subagent_result') {
            var isError = !!data.error;
            var running = b.querySelector('.subagent-inline.tool-inline-running[data-tool-name="' + CSS.escape(data.name || '') + '"]');
            if (running) {
                running.querySelector('.tool-spinner').outerHTML = isError ? '&#10007;' : '&#10003;';
                running.className = 'tool-inline-event subagent-inline subagent-inline-' + (isError ? 'error' : 'done');
                var label = running.querySelector('.tool-inline-task');
                if (label && data.result) {
                    var r = document.createElement('div');
                    r.className = 'tool-inline-subresult';
                    r.innerHTML = md(data.result);
                    running.replaceChild(r, label);
                } else if (label && data.error) {
                    label.textContent = data.error;
                    label.className = 'tool-inline-args';
                }
            }
            // Finalize the subagent's own streaming bubble
            var agentName = data.name || '';
            if (_subagentBubbles[agentName]) {
                finalizeSubagentBubble(agentName, data.result || '');
            }
            chatMsgs.scrollTop = chatMsgs.scrollHeight;
            return;
        } else {
            return;
        }

        b.appendChild(el);
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }

    function newChat() {
        if (isStreaming) return;
        currentSessionId = null;
        _subagentBubbles = {};
        sessionStorage.removeItem('agent_session_id');
        chatMsgs.innerHTML = '<div class="welcome-text">Ask the agent...</div>';
        chatIn.focus();
    }

    async function sendMsg() {
        const msg = chatIn.value.trim();
        if (!msg || isStreaming) return;
        chatIn.value = ''; chatIn.style.height = 'auto';
        btnSend.disabled = true; isStreaming = true;
        setAvatar('thinking');
        elDot.className = 'status-dot running';

        if (!currentSessionId) {
            currentSessionId = 'web_' + Math.random().toString(36).slice(2,10);
            sessionStorage.setItem('agent_session_id', currentSessionId);
        }
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
                        else if (ev.type === 'round_start') { startNewMainRound(); }
                        else if (ev.type === 'subagent_round_start') {
                            var sd = ev.data || {};
                            startNewSubagentRound(sd.agent_name || '');
                        }
                        else if (ev.type === 'subagent_token') {
                            setAvatar('speaking');
                            var sd = ev.data || {};
                            getOrCreateSubagentBubble(sd.agent_name || '', sd.agent_type || 'subagent');
                            streamSubagentToken(sd.agent_name || '', sd.content || '');
                        }
                        else if (ev.type === 'tool_start' || ev.type === 'tool_result' ||
                                 ev.type === 'subagent_start' || ev.type === 'subagent_result' ||
                                 ev.type === 'subagent_tool_start' || ev.type === 'subagent_tool_result') {
                            addToolEvent(ev.type, ev.data || {});
                        }
                    } catch {}
                }
            }
        } catch (err) {
            finishStream('Error: ' + err.message);
        } finally {
            // Clean up any unfinished subagent bubbles
            for (var an in _subagentBubbles) {
                finalizeSubagentBubble(an, _subagentBubbles[an].accumulated || '');
            }
            isStreaming = false; btnSend.disabled = false;
            setAvatar('idle');
            elDot.className = 'status-dot connected'; elStatus.textContent = 'ready';
            fetchAll(); chatIn.focus();
        }
    }

    // ==================== SESSIONS ====================
    async function fetchAgentSessions() {
        try {
            const r = await fetch(API + '/api/agent/sessions');
            if (!r.ok) { sessionsSessionList.innerHTML = '<div class="empty-state">Failed to load</div>'; return; }
            const d = await r.json();
            const sessions = d.sessions || [];
            sessionsTotalCount.textContent = d.total || 0;

            if (!sessions.length) {
                sessionsSessionList.innerHTML = '<div class="empty-state">No active sessions</div>';
                return;
            }

            const grouped = {};
            sessions.forEach(s => {
                const aid = s.agent_id || 'unknown';
                grouped[aid] = grouped[aid] || [];
                grouped[aid].push(s);
            });

            let html = '';
            Object.keys(grouped).forEach(aid => {
                html += '<div class="session-group-label">' + esc(aid) + '</div>';
                grouped[aid].forEach(s => {
                    const time = s.last_accessed ? new Date(s.last_accessed).toLocaleString() : '-';
                    html += '<div class="session-item" data-sid="' + esc(s.id) + '">' +
                        '<div class="session-item-id">' + esc(s.id) + '</div>' +
                        '<div class="session-item-meta">' +
                        '<span>' + s.messages + ' msgs</span>' +
                        '<span>' + esc(time) + '</span>' +
                        '</div></div>';
                });
            });

            sessionsSessionList.innerHTML = html;

            sessionsSessionList.querySelectorAll('.session-item').forEach(el => {
                el.addEventListener('click', () => loadSessionMessages(el.dataset.sid));
            });
        } catch (e) {
            sessionsSessionList.innerHTML = '<div class="empty-state">Error: ' + esc(e.message) + '</div>';
        }
    }

    async function loadSessionMessages(sessionId) {
        try {
            const r = await fetch(API + '/api/agent/sessions/' + encodeURIComponent(sessionId) + '/messages');
            if (!r.ok) { sessionMessagesList.innerHTML = '<div class="empty-state">Failed to load</div>'; return; }
            const d = await r.json();
            const msgs = d.messages || [];

            sessionsEmpty.style.display = 'none';
            sessionsMessages.style.display = 'flex';

            sessionMessagesHeader.innerHTML =
                '<span class="smh-agent">' + esc(d.agent_id || '') + '</span>' +
                '<span class="smh-id">' + esc(sessionId) + '</span>' +
                '<span class="smh-count">' + msgs.length + ' messages</span>';

            const agentId = d.agent_id || '';

            sessionMessagesList.innerHTML = msgs.map(m => {
                const role = m.role || '';

                if (role === 'tool') {
                    const toolName = m.name || 'tool';
                    let content = m.content || '';
                    let subName = '';
                    let isSubResult = false;
                    if (toolName === 'subagent') {
                        try { const j = JSON.parse(content); if (j.result) { content = j.result; isSubResult = true; } if (j.agent_id) subName = j.agent_id; } catch {}
                    }
                    const needCollapse = content.length > 500;
                    const short = needCollapse ? content.substring(0, 500) : content;
                    const renderHtml = isSubResult ? md(short) : '<pre>' + esc(short) + '</pre>';
                    const renderFull = isSubResult ? md(content) : '<pre>' + esc(content) + '</pre>';
                    return '<div class="smsg smsg-tool' + (isSubResult ? ' smsg-subresult' : '') + '">' +
                        '<div class="smsg-header"><span class="smsg-role">[' + esc(toolName) + ']' + (subName ? ' ' + esc(subName) : '') + '</span></div>' +
                        '<div class="smsg-tool-content collapsible" data-full="' + esc(renderFull).replace(/"/g, '&quot;') + '">' +
                        '<div class="collapsed-text">' + renderHtml + '</div>' +
                        (needCollapse ? '<button class="expand-btn" onclick="var c=this.parentElement;c.querySelector(\'.collapsed-text\').innerHTML=c.dataset.full;this.style.display=\'none\'">Show all (' + content.length + ' chars)</button>' : '') +
                        '</div></div>';
                }

                if (role === 'system') {
                    const content = m.content || '';
                    const needCollapse = content.length > 300;
                    const short = needCollapse ? content.substring(0, 300) : content;
                    const renderShort = md(short);
                    const renderFull = md(content);
                    return '<div class="smsg smsg-system">' +
                        '<div class="smsg-header"><span class="smsg-role">System</span></div>' +
                        '<div class="smsg-system-content collapsible" data-full="' + esc(renderFull).replace(/"/g, '&quot;') + '">' +
                        '<div class="collapsed-text">' + renderShort + '</div>' +
                        (needCollapse ? '<button class="expand-btn" onclick="var c=this.parentElement;c.querySelector(\'.collapsed-text\').innerHTML=c.dataset.full;this.style.display=\'none\'">Show all (' + content.length + ' chars)</button>' : '') +
                        '</div></div>';
                }

                const displayName = role === 'user' ? 'User' : (agentId || 'Assistant');

                if (role === 'assistant' && m.tool_calls) {
                    const calls = m.tool_calls.map(tc => {
                        const fn = tc.function || {};
                        const args = fn.arguments || '{}';
                        const needCollapse = args.length > 200;
                        const shortArgs = needCollapse ? args.substring(0, 200) : args;
                        return '<div class="smsg-tool-call"><span class="stc-name">' + esc(fn.name || '?') + '</span>' +
                            '<pre class="stc-args collapsible" data-full="' + esc(args).replace(/"/g, '&quot;') + '">' + esc(shortArgs) + '</pre>' +
                            (needCollapse ? '<button class="expand-btn" onclick="this.previousElementSibling.textContent=this.previousElementSibling.dataset.full,this.style.display=\'none\'">Show all</button>' : '') +
                            '</div>';
                    }).join('');
                    return '<div class="smsg smsg-assistant">' +
                        '<div class="smsg-header"><span class="smsg-role">' + esc(displayName) + '</span></div>' +
                        (m.content ? '<div class="smsg-content">' + md(m.content) + '</div>' : '') +
                        '<div class="smsg-tool-calls">' + calls + '</div></div>';
                }

                const isUser = role === 'user';
                return '<div class="smsg ' + (isUser ? 'smsg-user' : 'smsg-assistant') + '">' +
                    '<div class="smsg-header"><span class="smsg-role">' + esc(displayName) + '</span></div>' +
                    '<div class="smsg-content">' + (isUser ? esc(m.content || '') : md(m.content || '')) + '</div></div>';
            }).join('');

            sessionMessagesList.scrollTop = 0;
        } catch (e) {
            sessionMessagesList.innerHTML = '<div class="empty-state">Error: ' + esc(e.message) + '</div>';
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
        // Table
        html = html.replace(/^(\|.+\|)\n(\|[\s\-:|]+\|)\n((?:\|.+\|\n?)*)/gm, function(_, headerRow, sepRow, bodyRows) {
            var headers = headerRow.split('|').filter(function(c){return c.trim()!=='';});
            var hHtml = '<thead><tr>' + headers.map(function(h){return '<th>'+h.trim()+'</th>';}).join('') + '</tr></thead>';
            var rows = bodyRows.trim().split('\n').filter(function(r){return r.trim();});
            var bHtml = '<tbody>' + rows.map(function(row){
                var cells = row.split('|').filter(function(c){return c.trim()!=='';});
                return '<tr>' + cells.map(function(c){return '<td>'+c.trim()+'</td>';}).join('') + '</tr>';
            }).join('') + '</tbody>';
            return '<table>' + hHtml + bHtml + '</table>';
        });
        // Paragraphs: blank lines
        html = html.replace(/\n\n/g, '</p><p>');
        html = '<p>' + html + '</p>';
        // Clean empty paragraphs
        html = html.replace(/<p>\s*<\/p>/g, '');
        return html;
    }
})();
