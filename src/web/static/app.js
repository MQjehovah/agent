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
    const kanbanBody = $('#kanbanBody');
    const usersBody = $('#usersBody');
    const memoryBody = $('#memoryBody');
    const wsStatus = $('#wsStatus'), wsMemory = $('#wsMemory');
    const chatMsgs = $('#chatMessages'), chatIn = $('#chatInput'), btnSend = $('#sendBtn');
    const btnNewChat = $('#newChatBtn');
    const logContent = $('#logContent');
    let logStream = null; // 日志流 EventSource；提前声明，避免 switchTab 初始化时 TDZ
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
        loadAgents();
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
        bindKanban();
    }

    function switchTab(tab) {
        activeTab = tab;
        document.querySelectorAll('.main-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
        wsBody.style.display = tab === 'workspace' ? 'flex' : 'none';
        chatBody.style.display = tab === 'chat' ? 'flex' : 'none';
        kanbanBody.style.display = tab === 'kanban' ? 'flex' : 'none';
        sessionsBody.style.display = tab === 'sessions' ? 'flex' : 'none';
        logsBody.style.display = tab === 'logs' ? 'flex' : 'none';
        usersBody.style.display = tab === 'users' ? 'flex' : 'none';
        memoryBody.style.display = tab === 'memory' ? 'flex' : 'none';
        if (tab === 'chat') { chatIn.focus(); chatMsgs.scrollTop = chatMsgs.scrollHeight; }
        if (tab === 'kanban') { fetchKanban(); }
        if (tab === 'sessions') { fetchAgentSessions(); }
        if (tab === 'users') { rbacFetchAll(); }
        if (tab === 'memory') { fetchMemories(); fetchMemProposals(); }
        if (tab === 'logs') { startLogStream(); } else { stopLogStream(); }
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
        await Promise.all([fetchStatus(), fetchKanbanSidebar(), fetchTodos()]);
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
        } catch { elDot.className = 'status-dot'; elStatus.textContent = 'disconnected'; }
    }

    async function fetchKanbanSidebar() {
        try {
            const r = await fetch(API + '/api/kanban');
            if (!r.ok) {
                elTaskList.innerHTML = '<div class="empty-state">Kanban HTTP ' + r.status + '</div>';
                return;
            }
            const d = await r.json();
            const tasks = d.tasks || [];
            const stats = d.stats || {};
            const byCol = stats.by_column || {};

            elTasks.textContent = stats.total || 0;
            if (!tasks.length) {
                elTaskList.innerHTML = '<div class="empty-state">Kanban empty</div>';
            } else {
                elTaskList.innerHTML = tasks.map(t => {
                    const colColors = {backlog:'var(--text-muted)',todo:'var(--accent-orange)',in_progress:'var(--accent-blue)',done:'var(--accent-green)'};
                    let h = '<div class="task-card ' + t.column + '">';
                    h += '<div class="task-title">' + esc(t.title) + '</div>';
                    h += '<div class="task-meta">';
                    h += '<span style="color:' + (colColors[t.column]||'') + '">' + t.column.replace('_',' ') + '</span>';
                    if (t.assignee) h += '<span class="task-source">' + esc(t.assignee) + '</span>';
                    h += '<span class="task-source">' + esc(t.source === 'llm' ? 'AI' : t.source) + '</span>';
                    if (t.interval) h += '<span class="task-interval">' + fmti(t.interval) + '</span>';
                    h += '<span>' + fmt(t.created_at) + '</span>';
                    h += '</div></div>';
                    return h;
                }).join('');
            }
        } catch (e) {
            elTaskList.innerHTML = '<div class="empty-state">Fetch failed: ' + esc(e.message) + '</div>';
        }
    }

    // ==================== KANBAN BOARD ====================
    let kanbanTasks = [];
    let kanbanDragId = null;

    function bindKanban() {
        $('#kanbanRefreshBtn').addEventListener('click', fetchKanban);
        $('#kanbanNewBtn').addEventListener('click', () => showKanbanModal());
        $('#kanbanAddBtn').addEventListener('click', () => showKanbanModal());
        $('#kanbanModalCancel').addEventListener('click', hideKanbanModal);
        $('#kanbanModalSubmit').addEventListener('click', submitKanbanTask);

        document.querySelectorAll('.kanban-col-body').forEach(col => {
            col.addEventListener('dragover', e => { e.preventDefault(); col.classList.add('drag-over'); });
            col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
            col.addEventListener('drop', e => {
                e.preventDefault();
                col.classList.remove('drag-over');
                const id = e.dataTransfer.getData('text/plain');
                const column = col.parentElement.dataset.column;
                if (id && column) moveKanbanTask(id, column);
            });
        });
    }

    async function fetchKanban() {
        try {
            const r = await fetch(API + '/api/kanban');
            if (!r.ok) return;
            const d = await r.json();
            kanbanTasks = d.tasks || [];
            renderKanbanBoard();
        } catch {}
    }

    function renderKanbanBoard() {
        const cols = { backlog: [], todo: [], in_progress: [], done: [] };
        kanbanTasks.forEach(t => {
            if (cols[t.column]) cols[t.column].push(t);
        });

        const colBodies = {
            backlog: $('#kanbanColBacklog'),
            todo: $('#kanbanColTodo'),
            in_progress: $('#kanbanColInProgress'),
            done: $('#kanbanColDone'),
        };
        const colCounts = {
            backlog: $('#kanbanCountBacklog'),
            todo: $('#kanbanCountTodo'),
            in_progress: $('#kanbanCountInProgress'),
            done: $('#kanbanCountDone'),
        };

        Object.keys(cols).forEach(col => {
            colCounts[col].textContent = cols[col].length;
            colBodies[col].innerHTML = cols[col].length ? cols[col].map(t => {
                const pCls = 'p' + t.priority;
                const pLabels = {1:'High',2:'Med',3:'Low'};
                let h = '<div class="kanban-card" draggable="true" data-id="' + esc(t.id) + '">';
                h += '<button class="kanban-card-del" data-id="' + esc(t.id) + '" title="Delete">&times;</button>';
                h += '<div class="kanban-card-title">' + esc(t.title) + '</div>';
                if (t.description) h += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;line-height:1.4">' + esc(t.description.substring(0, 100)) + '</div>';
                h += '<div class="kanban-card-meta">';
                h += '<span class="kanban-card-priority ' + pCls + '">' + (pLabels[t.priority]||'Low') + '</span>';
                if (t.assignee) h += '<span class="kanban-card-assignee">' + esc(t.assignee) + '</span>';
                h += '<span class="kanban-card-source">' + esc(t.source) + '</span>';
                h += '<span>' + fmt(t.created_at) + '</span>';
                h += '</div></div>';
                return h;
            }).join('') : '<div class="empty-state">Empty</div>';

            colBodies[col].querySelectorAll('.kanban-card[draggable]').forEach(card => {
                card.addEventListener('dragstart', e => {
                    kanbanDragId = card.dataset.id;
                    e.dataTransfer.setData('text/plain', card.dataset.id);
                    card.classList.add('dragging');
                });
                card.addEventListener('dragend', () => card.classList.remove('dragging'));
            });

            colBodies[col].querySelectorAll('.kanban-card-del').forEach(btn => {
                btn.addEventListener('click', e => {
                    e.stopPropagation();
                    deleteKanbanTask(btn.dataset.id);
                });
            });
        });
    }

    async function moveKanbanTask(id, column) {
        try {
            await fetch(API + '/api/kanban/' + id + '/move', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({column: column}),
            });
            await fetchKanban();
            await fetchKanbanSidebar();
        } catch {}
    }

    async function deleteKanbanTask(id) {
        try {
            await fetch(API + '/api/kanban/' + id, {method: 'DELETE'});
            await fetchKanban();
            await fetchKanbanSidebar();
        } catch {}
    }

    function showKanbanModal() {
        $('#kanbanModal').style.display = 'flex';
        $('#kanbanInputTitle').value = '';
        $('#kanbanInputDesc').value = '';
        $('#kanbanInputPriority').value = '3';
        $('#kanbanInputColumn').value = 'backlog';
        $('#kanbanInputTitle').focus();
    }

    function hideKanbanModal() {
        $('#kanbanModal').style.display = 'none';
    }

    async function submitKanbanTask() {
        const title = $('#kanbanInputTitle').value.trim();
        if (!title) { $('#kanbanInputTitle').focus(); return; }
        const body = {
            title: title,
            description: $('#kanbanInputDesc').value.trim(),
            priority: parseInt($('#kanbanInputPriority').value) || 3,
            column: $('#kanbanInputColumn').value,
        };
        try {
            const r = await fetch(API + '/api/kanban', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            });
            if (r.ok) {
                hideKanbanModal();
                await fetchKanban();
                await fetchKanbanSidebar();
            }
        } catch {}
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

    // ==================== LOGS ====================
    function startLogStream() {
        if (logStream) return;
        logContent.textContent = '✅ 已连接实时日志流，等待 agent 活动（系统空闲时无日志输出）…\n';
        try {
            logStream = new EventSource(API + '/api/logs/stream');
            logStream.onopen = () => {
                // 连接已建立；首条真实日志到来后会替换提示
            };
            logStream.onmessage = (e) => {
                try {
                    const d = JSON.parse(e.data);
                    if (d.line) {
                        // 首条真实日志到来时，若仍为初始提示则清掉
                        if (logContent.textContent.startsWith('✅ 已连接实时日志流')) {
                            logContent.textContent = '';
                        }
                        logContent.textContent += d.line + '\n';
                        // 限制最大行数，防止 DOM 无限膨胀
                        const lines = logContent.textContent.split('\n');
                        if (lines.length > 1000) {
                            logContent.textContent = lines.slice(-1000).join('\n');
                        }
                        logContent.scrollTop = logContent.scrollHeight;
                    }
                    // heartbeat：连接保活，无需渲染
                } catch {}
            };
            logStream.onerror = () => { /* EventSource 会自动重连 */ };
        } catch (e) {
            logContent.textContent = 'Failed to connect log stream: ' + e.message;
        }
    }
    function stopLogStream() {
        if (logStream) { logStream.close(); logStream = null; }
    }

    // ==================== SESSIONS ====================
    async function fetchAgentSessions() {
        try {
            const r = await fetch(API + '/api/agent/sessions/history?limit=20');
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

    // ==================== AGENT EDITOR ====================
    var _currentAgent = null;
    var _currentSkill = null;
    var wsAgents = $('#wsAgents');

    async function loadAgents() {
        try {
            var r = await fetch(API + '/api/agents');
            if (!r.ok) { wsAgents.innerHTML = '<div class="empty-state">Failed to load</div>'; return; }
            var d = await r.json();
            var agents = Array.isArray(d) ? d : (d.agents || []);
            if (!agents.length) { wsAgents.innerHTML = '<div class="empty-state">No agents</div>'; return; }
            wsAgents.innerHTML = agents.map(function(a) {
                return '<div class="ws-agent-item" data-agent="' + esc(a.name) + '">' +
                    '<span class="ws-agent-name">' + esc(a.name) + '</span>' +
                    '<span class="ws-agent-skills-count">' + (a.skills ? a.skills.length : 0) + ' skills</span>' +
                    '</div>';
            }).join('');
            wsAgents.querySelectorAll('.ws-agent-item').forEach(function(el) {
                el.addEventListener('click', function() { openAgentEditor(el.dataset.agent); });
            });
        } catch (e) {
            wsAgents.innerHTML = '<div class="empty-state">Error: ' + esc(e.message) + '</div>';
        }
    }

    var _mdModes = {};

    function renderMd(text) {
        var body = text || '';
        var fmHtml = '';
        var fmMatch = body.match(/^---\r?\n([\s\S]*?)\r?\n---/);
        if (fmMatch) {
            body = body.slice(fmMatch[0].length);
            var props = [];
            fmMatch[1].split('\n').forEach(function(line) {
                var m = line.match(/^(\w+):\s*(.*)/);
                if (m) {
                    var val = m[2].trim();
                    if (val === '|') return;
                    props.push({k: m[1], v: val});
                }
            });
            if (props.length) {
                fmHtml = '<table class="fm-table"><tbody>' +
                    props.map(function(p) {
                        return '<tr><th>' + esc(p.k) + '</th><td>' + esc(p.v) + '</td></tr>';
                    }).join('') +
                    '</tbody></table>';
            }
        }
        try { return fmHtml + marked.parse(body); } catch { return fmHtml + esc(body); }
    }

    function updateMdPreview(textareaId, previewId) {
        var el = $('#' + textareaId);
        var pv = $('#' + previewId);
        if (!el || !pv) return;
        pv.innerHTML = renderMd(el.value || '');
    }

    function setMdMode(textareaId, mode) {
        _mdModes[textareaId] = mode;
        var el = $('#' + textareaId);
        var wrap = el ? el.closest('.md-editor-wrap') : null;
        if (!wrap) return;
        wrap.className = 'md-editor-wrap mode-' + mode;
        var pvId = textareaId.replace('Text', 'Preview');
        if (mode !== 'edit') updateMdPreview(textareaId, pvId);
    }

    function initMdEditor(textareaId) {
        _mdModes[textareaId] = 'edit';
        var ta = $('#' + textareaId);
        if (!ta) return;
        var wrap = ta.closest('.md-editor-wrap');
        if (wrap) wrap.className = 'md-editor-wrap mode-edit';
        var pvId = textareaId.replace('Text', 'Preview');
        ta.addEventListener('input', function() {
            if (_mdModes[textareaId] !== 'edit') updateMdPreview(textareaId, pvId);
        });
    }

    function openAgentEditor(name) {
        _currentAgent = name;
        _currentSkill = null;
        $('#agentEditorTitle').textContent = name;
        $('#agentEditorModal').style.display = 'flex';
        $('#agentSkillEditor').style.display = 'none';
        switchAgentTab('prompt');
        loadAgentPrompt(name);
    }

    function switchAgentTab(tab) {
        document.querySelectorAll('.agent-tab').forEach(function(t) {
            t.classList.toggle('active', t.dataset.agentTab === tab);
        });
        $('#agentPromptPanel').style.display = tab === 'prompt' ? '' : 'none';
        $('#agentSkillsPanel').style.display = tab === 'skills' ? '' : 'none';
        if (tab === 'skills' && _currentAgent) loadAgentSkills(_currentAgent);
    }

    async function loadAgentPrompt(name) {
        try {
            var r = await fetch(API + '/api/agents/' + encodeURIComponent(name) + '/prompt');
            if (!r.ok) return;
            var d = await r.json();
            $('#agentPromptText').value = d.content || '';
            $('#agentPromptStatus').textContent = '';
            updateMdPreview('agentPromptText', 'agentPromptPreview');
        } catch {}
    }

    async function loadAgentSkills(name) {
        try {
            var r = await fetch(API + '/api/agents/' + encodeURIComponent(name) + '/skills');
            if (!r.ok) return;
            var d = await r.json();
            var skills = Array.isArray(d) ? d : (d.skills || []);
            var list = $('#agentSkillsList');
            if (!skills.length) {
                list.innerHTML = '<div class="empty-state" style="width:100%">No skills</div>';
            } else {
                list.innerHTML = skills.map(function(s) {
                    return '<span class="agent-skill-chip" data-skill="' + esc(s) + '">' + esc(s) + '</span>';
                }).join('');
                list.querySelectorAll('.agent-skill-chip').forEach(function(chip) {
                    chip.addEventListener('click', function() { loadSkillContent(chip.dataset.skill); });
                });
            }
            $('#agentSkillEditor').style.display = 'none';
            _currentSkill = null;
        } catch {}
    }

    async function loadSkillContent(skill) {
        _currentSkill = skill;
        document.querySelectorAll('.agent-skill-chip').forEach(function(c) {
            c.classList.toggle('active', c.dataset.skill === skill);
        });
        try {
            var r = await fetch(API + '/api/agents/' + encodeURIComponent(_currentAgent) + '/skills/' + encodeURIComponent(skill));
            if (!r.ok) return;
            var d = await r.json();
            $('#agentSkillEditorName').textContent = skill;
            $('#agentSkillText').value = d.content || '';
            $('#agentSkillEditor').style.display = '';
            $('#agentSkillStatus').textContent = '';
            updateMdPreview('agentSkillText', 'agentSkillPreview');
        } catch {}
    }

    document.addEventListener('DOMContentLoaded', function() {
        initMdEditor('agentPromptText');
        initMdEditor('agentSkillText');
        document.addEventListener('click', function(e) {
            if (e.target.classList.contains('md-mode-btn') && e.target.dataset.target) {
                var target = e.target.dataset.target;
                e.target.parentElement.querySelectorAll('.md-mode-btn').forEach(function(b) { b.classList.remove('active'); });
                e.target.classList.add('active');
                setMdMode(target, e.target.dataset.mode);
            }
        });
        $('#agentEditorClose').addEventListener('click', function() {
            $('#agentEditorModal').style.display = 'none';
        });
        $('#agentEditorModal').addEventListener('click', function(e) {
            if (e.target === this) this.style.display = 'none';
        });
        document.querySelectorAll('.agent-tab').forEach(function(t) {
            t.addEventListener('click', function() { switchAgentTab(t.dataset.agentTab); });
        });
        $('#agentPromptSave').addEventListener('click', async function() {
            if (!_currentAgent) return;
            var content = $('#agentPromptText').value;
            try {
                var r = await fetch(API + '/api/agents/' + encodeURIComponent(_currentAgent) + '/prompt', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({content: content}),
                });
                if (r.ok) { $('#agentPromptStatus').textContent = 'Saved'; setTimeout(function() { $('#agentPromptStatus').textContent = ''; }, 2000); }
            } catch {}
        });
        $('#agentSkillSave').addEventListener('click', async function() {
            if (!_currentAgent || !_currentSkill) return;
            var content = $('#agentSkillText').value;
            try {
                var r = await fetch(API + '/api/agents/' + encodeURIComponent(_currentAgent) + '/skills/' + encodeURIComponent(_currentSkill), {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({content: content}),
                });
                if (r.ok) { $('#agentSkillStatus').textContent = 'Saved'; setTimeout(function() { $('#agentSkillStatus').textContent = ''; }, 2000); }
            } catch {}
        });
        $('#agentNewSkillBtn').addEventListener('click', function() {
            $('#newSkillModal').style.display = 'flex';
            $('#newSkillInput').value = '';
            $('#newSkillInput').focus();
        });
        $('#newSkillClose').addEventListener('click', function() {
            $('#newSkillModal').style.display = 'none';
        });
        $('#newSkillCancel').addEventListener('click', function() {
            $('#newSkillModal').style.display = 'none';
        });
        $('#newSkillModal').addEventListener('click', function(e) {
            if (e.target === this) this.style.display = 'none';
        });
        $('#newSkillSubmit').addEventListener('click', async function() {
            var name = $('#newSkillInput').value.trim();
            if (!name || !_currentAgent) { $('#newSkillInput').focus(); return; }
            var template = '# ' + esc(name) + '\n\n';
            try {
                var r = await fetch(API + '/api/agents/' + encodeURIComponent(_currentAgent) + '/skills', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name, content: template}),
                });
                if (r.ok) {
                    $('#newSkillModal').style.display = 'none';
                    await loadAgentSkills(_currentAgent);
                    loadSkillContent(name);
                }
            } catch {}
        });
        $('#agentSkillDelBtn').addEventListener('click', async function() {
            if (!_currentAgent || !_currentSkill) return;
            if (!confirm('Delete skill "' + _currentSkill + '"?')) return;
            try {
                var r = await fetch(API + '/api/agents/' + encodeURIComponent(_currentAgent) + '/skills/' + encodeURIComponent(_currentSkill), {
                    method: 'DELETE',
                });
                if (r.ok) {
                    _currentSkill = null;
                    $('#agentSkillEditor').style.display = 'none';
                    await loadAgentSkills(_currentAgent);
                }
            } catch {}
        });
    });

    // ==================== RBAC USERS ====================
    let _rbacRoles = [];
    let _rbacUsers = [];
    let _rbacEditIdentities = [];

    function rbacFetchAll() {
        rbacFetchRoles();
        rbacFetchUsers();
    }

    async function rbacFetchRoles() {
        try {
            var r = await fetch(API + '/api/rbac/roles');
            if (!r.ok) return;
            var data = await r.json();
            _rbacRoles = data.roles || [];
            rbacRenderRoles();
            rbacRenderRoleSelect();
        } catch {}
    }

    async function rbacFetchUsers() {
        try {
            var r = await fetch(API + '/api/rbac/users');
            if (!r.ok) return;
            var data = await r.json();
            _rbacUsers = data.users || [];
            rbacRenderUsers();
        } catch {}
    }

    function rbacRenderRoles() {
        var html = '';
        _rbacRoles.forEach(function(role) {
            var tools = role.allowed_tools;
            var toolsStr = tools[0] === '*' ? 'All tools' : tools.join(', ') || 'No tools';
            var agentsStr = role.allowed_agents[0] === '*' ? 'All agents' : role.allowed_agents.join(', ') || 'No agents';
            html += '<div class="users-role-card" data-role="' + esc(role.name) + '">'
                + '<div class="users-role-name">' + esc(role.name) + '</div>'
                + '<div class="users-role-desc">' + esc(role.description || '') + '</div>'
                + '<div class="users-role-tools">' + esc(toolsStr) + ' | ' + esc(agentsStr) + '</div>'
                + '</div>';
        });
        if (!html) html = '<div class="empty-state">No roles</div>';
        $('#rbacRoleList').innerHTML = html;
        document.querySelectorAll('.users-role-card').forEach(function(card) {
            card.addEventListener('click', function() {
                document.querySelectorAll('.users-role-card').forEach(function(c) { c.classList.remove('active'); });
                card.classList.add('active');
                var name = card.dataset.role;
                rbacOpenRoleModal(name);
            });
        });
    }

    function rbacRenderRoleSelect() {
        var sel = $('#rbacUserInputRole');
        sel.innerHTML = '';
        _rbacRoles.forEach(function(role) {
            var opt = document.createElement('option');
            opt.value = role.name;
            opt.textContent = role.name;
            sel.appendChild(opt);
        });
    }

    function rbacRenderUsers() {
        var html = '';
        _rbacUsers.forEach(function(u) {
            var statusClass = u.status === 'active' ? 'users-status-active' : 'users-status-disabled';
            var statusText = u.status === 'active' ? 'Active' : 'Disabled';
            var idents = (u.identities || []).map(function(id) {
                return '<span class="users-identity-tag">' + esc(id.platform) + ':' + esc(id.platform_uid)
                    + ' <span class="users-identity-remove" data-id="' + id.id + '">&times;</span></span>';
            }).join('') || '<span style="color:var(--text-muted)">-</span>';
            html += '<tr data-uid="' + u.id + '">'
                + '<td>' + u.id + '</td>'
                + '<td>' + esc(u.name) + '</td>'
                + '<td>' + esc(u.department || '') + '</td>'
                + '<td>' + esc(u.role) + '</td>'
                + '<td>' + idents + '</td>'
                + '<td><span class="users-status ' + statusClass + '">' + statusText + '</span></td>'
                + '<td class="users-actions">'
                + '<button class="users-btn users-btn-sm rbac-edit-user">Edit</button>'
                + '<button class="users-btn users-btn-sm rbac-bind-user">Bind</button>'
                + '<button class="users-btn users-btn-sm rbac-toggle-user">' + (u.status === 'active' ? 'Disable' : 'Enable') + '</button>'
                + '<button class="users-btn users-btn-sm users-btn-danger rbac-del-user">Del</button>'
                + '</td></tr>';
        });
        if (!html) html = '<tr><td colspan="7" class="empty-state">No users</td></tr>';
        $('#rbacUserTbody').innerHTML = html;

        document.querySelectorAll('.rbac-edit-user').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var uid = parseInt(btn.closest('tr').dataset.uid);
                rbacOpenUserModal(uid);
            });
        });
        document.querySelectorAll('.rbac-bind-user').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var uid = parseInt(btn.closest('tr').dataset.uid);
                rbacOpenBindModal(uid);
            });
        });
        document.querySelectorAll('.rbac-toggle-user').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var uid = parseInt(btn.closest('tr').dataset.uid);
                rbacToggleUser(uid);
            });
        });
        document.querySelectorAll('.rbac-del-user').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var uid = parseInt(btn.closest('tr').dataset.uid);
                if (confirm('Delete this user?')) rbacDeleteUser(uid);
            });
        });
        document.querySelectorAll('.users-identity-remove').forEach(function(span) {
            span.addEventListener('click', function(e) {
                e.stopPropagation();
                var iid = parseInt(span.dataset.id);
                rbacUnbindIdentity(iid);
            });
        });
    }

    function rbacOpenUserModal(userId) {
        var modal = $('#rbacUserModal');
        var user = userId ? _rbacUsers.find(function(u) { return u.id === userId; }) : null;
        $('#rbacUserModalTitle').textContent = user ? 'Edit User' : 'New User';
        $('#rbacUserEditId').value = user ? user.id : '';
        $('#rbacUserInputName').value = user ? user.name : '';
        $('#rbacUserInputDept').value = user ? (user.department || '') : '';
        $('#rbacUserInputRole').value = user ? user.role : 'default';
        _rbacEditIdentities = user ? (user.identities || []).map(function(id) {
            return { platform: id.platform, platform_uid: id.platform_uid };
        }) : [];
        rbacRenderEditIdentities();
        modal.style.display = 'flex';
    }

    function rbacRenderEditIdentities() {
        var html = '';
        _rbacEditIdentities.forEach(function(ident, i) {
            html += '<div class="users-edit-identity-row">'
                + '<select data-idx="' + i + '" class="rbac-ident-platform">'
                + '<option value="dingtalk"' + (ident.platform === 'dingtalk' ? ' selected' : '') + '>DingTalk</option>'
                + '<option value="feishu"' + (ident.platform === 'feishu' ? ' selected' : '') + '>Feishu</option>'
                + '<option value="webhook"' + (ident.platform === 'webhook' ? ' selected' : '') + '>Webhook</option>'
                + '</select>'
                + '<input data-idx="' + i + '" class="rbac-ident-uid" value="' + esc(ident.platform_uid) + '" placeholder="Platform User ID" />'
                + '<button class="users-btn users-btn-sm users-btn-danger rbac-remove-ident" data-idx="' + i + '">&times;</button>'
                + '</div>';
        });
        $('#rbacIdentityList').innerHTML = html;
        document.querySelectorAll('.rbac-ident-platform').forEach(function(sel) {
            sel.addEventListener('change', function() {
                _rbacEditIdentities[parseInt(sel.dataset.idx)].platform = sel.value;
            });
        });
        document.querySelectorAll('.rbac-ident-uid').forEach(function(inp) {
            inp.addEventListener('input', function() {
                _rbacEditIdentities[parseInt(inp.dataset.idx)].platform_uid = inp.value;
            });
        });
        document.querySelectorAll('.rbac-remove-ident').forEach(function(btn) {
            btn.addEventListener('click', function() {
                _rbacEditIdentities.splice(parseInt(btn.dataset.idx), 1);
                rbacRenderEditIdentities();
            });
        });
    }

    $('#rbacAddIdentityBtn').addEventListener('click', function() {
        _rbacEditIdentities.push({ platform: 'dingtalk', platform_uid: '' });
        rbacRenderEditIdentities();
    });

    $('#rbacUserModalClose').addEventListener('click', function() {
        $('#rbacUserModal').style.display = 'none';
    });
    $('#rbacUserModalCancel').addEventListener('click', function() {
        $('#rbacUserModal').style.display = 'none';
    });
    $('#rbacUserModal').addEventListener('click', function(e) {
        if (e.target === this) this.style.display = 'none';
    });
    $('#rbacNewUserBtn').addEventListener('click', function() { rbacOpenUserModal(null); });
    $('#rbacUserModalSubmit').addEventListener('click', async function() {
        var editId = $('#rbacUserEditId').value;
        var name = $('#rbacUserInputName').value.trim();
        var dept = $('#rbacUserInputDept').value.trim();
        var role = $('#rbacUserInputRole').value;
        if (!name) { $('#rbacUserInputName').focus(); return; }
        try {
            var r;
            if (editId) {
                r = await fetch(API + '/api/rbac/users/' + editId, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ name: name, department: dept, role: role }),
                });
            } else {
                r = await fetch(API + '/api/rbac/users', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ name: name, department: dept, role: role, identities: _rbacEditIdentities.filter(function(i) { return i.platform_uid; }) }),
                });
            }
            if (r.ok) {
                $('#rbacUserModal').style.display = 'none';
                rbacFetchUsers();
            } else {
                var err = await r.json();
                alert(err.error || 'Failed');
            }
        } catch {}
    });

    function rbacOpenRoleModal(name) {
        var role = name ? _rbacRoles.find(function(r) { return r.name === name; }) : null;
        var modal = $('#rbacRoleModal');
        $('#rbacRoleModalTitle').textContent = role ? 'Edit Role' : 'New Role';
        $('#rbacRoleEditName').value = role ? role.name : '';
        $('#rbacRoleInputName').value = role ? role.name : '';
        $('#rbacRoleInputName').disabled = !!role;
        $('#rbacRoleInputDesc').value = role ? (role.description || '') : '';
        $('#rbacRoleInputTools').value = role ? JSON.stringify(role.allowed_tools) : '';
        $('#rbacRoleInputAgents').value = role ? JSON.stringify(role.allowed_agents) : '';
        modal.style.display = 'flex';
    }

    $('#rbacRoleModalClose').addEventListener('click', function() {
        $('#rbacRoleModal').style.display = 'none';
        $('#rbacRoleInputName').disabled = false;
    });
    $('#rbacRoleModalCancel').addEventListener('click', function() {
        $('#rbacRoleModal').style.display = 'none';
        $('#rbacRoleInputName').disabled = false;
    });
    $('#rbacRoleModal').addEventListener('click', function(e) {
        if (e.target === this) { this.style.display = 'none'; $('#rbacRoleInputName').disabled = false; }
    });
    $('#rbacNewRoleBtn').addEventListener('click', function() { rbacOpenRoleModal(null); });
    $('#rbacRoleModalSubmit').addEventListener('click', async function() {
        var editName = $('#rbacRoleEditName').value;
        var name = $('#rbacRoleInputName').value.trim();
        var desc = $('#rbacRoleInputDesc').value.trim();
        var toolsStr = $('#rbacRoleInputTools').value.trim();
        var agentsStr = $('#rbacRoleInputAgents').value.trim();
        if (!name) { $('#rbacRoleInputName').focus(); return; }
        var tools, agents;
        try { tools = toolsStr ? JSON.parse(toolsStr) : []; } catch { alert('Invalid tools JSON'); return; }
        try { agents = agentsStr ? JSON.parse(agentsStr) : []; } catch { alert('Invalid agents JSON'); return; }
        try {
            var r;
            if (editName) {
                r = await fetch(API + '/api/rbac/roles/' + encodeURIComponent(editName), {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ description: desc, allowed_tools: tools, allowed_agents: agents }),
                });
            } else {
                r = await fetch(API + '/api/rbac/roles', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ name: name, description: desc, allowed_tools: tools, allowed_agents: agents }),
                });
            }
            if (r.ok) {
                $('#rbacRoleModal').style.display = 'none';
                $('#rbacRoleInputName').disabled = false;
                rbacFetchRoles();
            } else {
                var err = await r.json();
                alert(err.error || 'Failed');
            }
        } catch {}
    });

    function rbacOpenBindModal(userId) {
        $('#rbacBindUserId').value = userId;
        $('#rbacBindPlatform').value = 'dingtalk';
        $('#rbacBindUid').value = '';
        $('#rbacBindModal').style.display = 'flex';
    }

    $('#rbacBindModalClose').addEventListener('click', function() {
        $('#rbacBindModal').style.display = 'none';
    });
    $('#rbacBindModalCancel').addEventListener('click', function() {
        $('#rbacBindModal').style.display = 'none';
    });
    $('#rbacBindModal').addEventListener('click', function(e) {
        if (e.target === this) this.style.display = 'none';
    });
    $('#rbacBindModalSubmit').addEventListener('click', async function() {
        var uid = $('#rbacBindUserId').value;
        var platform = $('#rbacBindPlatform').value;
        var platformUid = $('#rbacBindUid').value.trim();
        if (!platformUid) { $('#rbacBindUid').focus(); return; }
        try {
            var r = await fetch(API + '/api/rbac/users/' + uid + '/identities', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ platform: platform, platform_uid: platformUid }),
            });
            if (r.ok) {
                $('#rbacBindModal').style.display = 'none';
                rbacFetchUsers();
            }
        } catch {}
    });

    async function rbacToggleUser(uid) {
        try {
            await fetch(API + '/api/rbac/users/' + uid + '/toggle', { method: 'POST' });
            rbacFetchUsers();
        } catch {}
    }

    async function rbacDeleteUser(uid) {
        try {
            await fetch(API + '/api/rbac/users/' + uid, { method: 'DELETE' });
            rbacFetchUsers();
        } catch {}
    }

    async function rbacUnbindIdentity(identityId) {
        try {
            await fetch(API + '/api/rbac/identities/' + identityId, { method: 'DELETE' });
            rbacFetchUsers();
        } catch {}
    }

    // ==================== MEMORY 记忆管理 ====================
    const MEM_CAT_LABELS = {
        preference: '用户偏好', key_info: '关键信息', todo: '待办事项',
        failure_lesson: '避坑经验', correction: '用户纠正',
        reflection: '自学习', knowledge: '通用知识'
    };
    const memList = $('#memList');
    const memCountEl = $('#memCount');
    const memProposalsBox = $('#memProposalsBox');
    const memProposalsList = $('#memProposalsList');
    const memPropCount = $('#memPropCount');
    let _memCache = {};

    async function fetchMemories() {
        const params = new URLSearchParams({ limit: '200' });
        const scope = $('#memFilterScope').value; if (scope) params.set('scope', scope);
        const cat = $('#memFilterCategory').value; if (cat) params.set('category', cat);
        const owner = $('#memFilterOwner').value.trim(); if (owner) params.set('owner_id', owner);
        const q = $('#memFilterKeyword').value.trim(); if (q) params.set('q', q);
        try {
            const r = await fetch(API + '/api/memories?' + params.toString());
            if (!r.ok) { memList.innerHTML = '<div class="empty-state">加载失败</div>'; return; }
            const d = await r.json();
            memCountEl.textContent = '共 ' + d.total + ' 条';
            const items = d.memories || [];
            _memCache = {}; items.forEach(m => { _memCache[m.id] = m; });
            if (!items.length) { memList.innerHTML = '<div class="empty-state">暂无记忆</div>'; return; }
            memList.innerHTML = items.map(renderMemCard).join('');
            memList.querySelectorAll('.mem-edit').forEach(b => b.onclick = () => openMemoryModal(b.dataset.id));
            memList.querySelectorAll('.mem-del').forEach(b => b.onclick = () => deleteMemory(b.dataset.id));
        } catch (e) {
            memList.innerHTML = '<div class="empty-state">错误: ' + esc(e.message) + '</div>';
        }
    }

    function renderMemCard(m) {
        const cat = MEM_CAT_LABELS[m.category] || m.category;
        const scopeBadge = m.scope === 'global'
            ? '<span class="mem-badge mem-badge-global">global</span>'
            : '<span class="mem-badge mem-badge-user">user</span>';
        const owner = m.owner_id ? ' <span class="mem-owner">@' + esc(m.owner_id) + '</span>' : '';
        return '<div class="mem-card">' +
            '<div class="mem-card-head">' + scopeBadge +
                '<span class="mem-cat">' + esc(cat) + '</span>' + owner +
                '<span class="mem-imp" title="重要度">★' + esc(String(m.importance)) + '</span>' +
                '<span class="mem-card-actions">' +
                    '<button class="mem-edit" data-id="' + esc(String(m.id)) + '">编辑</button>' +
                    '<button class="mem-del" data-id="' + esc(String(m.id)) + '">删除</button>' +
                '</span>' +
            '</div>' +
            '<div class="mem-card-content">' + esc(m.content) + '</div>' +
            '<div class="mem-card-meta">' + esc(m.source || '') + (m.updated_at ? ' · ' + esc(m.updated_at) : '') + '</div>' +
        '</div>';
    }

    async function fetchMemProposals() {
        try {
            const r = await fetch(API + '/api/memory/proposals?status=pending');
            if (!r.ok) return;
            const d = await r.json();
            const items = d.proposals || [];
            if (!items.length) { memProposalsBox.style.display = 'none'; return; }
            memProposalsBox.style.display = 'block';
            memPropCount.textContent = '(' + items.length + ')';
            memProposalsList.innerHTML = items.map(p =>
                '<div class="mem-prop">' +
                    '<div class="mem-prop-content">' + esc(p.content) + '</div>' +
                    '<div class="mem-prop-meta">' + esc(p.reason || '') + (p.created_at ? ' · ' + esc(p.created_at) : '') + '</div>' +
                    '<div class="mem-prop-actions">' +
                        '<button class="mem-btn mem-btn-primary mem-approve" data-id="' + p.id + '">批准</button>' +
                        '<button class="mem-btn mem-reject" data-id="' + p.id + '">驳回</button>' +
                    '</div>' +
                '</div>').join('');
            memProposalsList.querySelectorAll('.mem-approve').forEach(b => b.onclick = () => memApprove(b.dataset.id));
            memProposalsList.querySelectorAll('.mem-reject').forEach(b => b.onclick = () => memReject(b.dataset.id));
        } catch {}
    }

    async function memApprove(id) {
        try { await fetch(API + '/api/memory/proposals/' + id + '/approve', { method: 'POST' }); } catch {}
        fetchMemProposals(); fetchMemories();
    }
    async function memReject(id) {
        try { await fetch(API + '/api/memory/proposals/' + id + '/reject', { method: 'POST' }); } catch {}
        fetchMemProposals();
    }

    function openMemoryModal(id) {
        const m = id ? _memCache[id] : null;
        $('#memoryModalTitle').textContent = id ? '编辑记忆' : '新增记忆';
        $('#memEditId').value = id || '';
        $('#memScope').value = m ? m.scope : 'global';
        $('#memOwner').value = m ? (m.owner_id || '') : '';
        $('#memCategory').value = m ? m.category : 'knowledge';
        $('#memImportance').value = m ? m.importance : 3;
        $('#memContent').value = m ? m.content : '';
        $('#memoryModal').style.display = 'flex';
    }

    async function saveMemory() {
        const id = $('#memEditId').value;
        const body = {
            scope: $('#memScope').value,
            owner_id: $('#memOwner').value.trim(),
            category: $('#memCategory').value,
            importance: parseInt($('#memImportance').value) || 3,
            content: $('#memContent').value.trim(),
        };
        if (!body.content) { alert('内容不能为空'); return; }
        try {
            const url = id ? API + '/api/memories/' + id : API + '/api/memories';
            const method = id ? 'PUT' : 'POST';
            const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            if (!r.ok) { const e = await r.json().catch(() => ({})); alert('保存失败: ' + (e.error || r.status)); return; }
            $('#memoryModal').style.display = 'none';
            fetchMemories();
        } catch (e) { alert('保存失败: ' + e.message); }
    }

    async function deleteMemory(id) {
        if (!confirm('确认删除这条记忆？')) return;
        try { await fetch(API + '/api/memories/' + id, { method: 'DELETE' }); } catch {}
        fetchMemories();
    }

    // memory 事件绑定
    $('#memSearchBtn').onclick = fetchMemories;
    $('#memFilterKeyword').addEventListener('keydown', e => { if (e.key === 'Enter') fetchMemories(); });
    $('#memNewBtn').onclick = () => openMemoryModal('');
    $('#memoryModalClose').onclick = () => { $('#memoryModal').style.display = 'none'; };
    $('#memoryModalCancel').onclick = () => { $('#memoryModal').style.display = 'none'; };
    $('#memoryModalSave').onclick = saveMemory;

})();
