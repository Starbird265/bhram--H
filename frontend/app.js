const API_BASE = '/api';
let logsInterval = null;

document.addEventListener('DOMContentLoaded', () => {
    loadConfig();
    checkStatus();
    fetchStats();
    fetchSkills();
});

async function loadConfig() {
    try {
        const response = await fetch(`${API_BASE}/config`);
        const data = await response.json();
        document.getElementById('data-sources').value = (data.data_sources || []).join('\n');
        document.getElementById('openai-key').value = data.openai_key || '';
        document.getElementById('slack-channel').value = data.slack_channel || '';
    } catch (error) {
        console.error('Error loading config:', error);
    }
}

async function saveConfig() {
    const btn = document.getElementById('save-config-btn');
    btn.textContent = 'Saving...';
    
    const dataSourcesRaw = document.getElementById('data-sources').value;
    const config = {
        data_sources: dataSourcesRaw.split('\n').map(s => s.trim()).filter(s => s),
        openai_key: document.getElementById('openai-key').value,
        slack_channel: document.getElementById('slack-channel').value
    };

    try {
        const response = await fetch(`${API_BASE}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });
        
        if (response.ok) {
            btn.textContent = 'Saved!';
            setTimeout(() => btn.textContent = 'Save Configuration', 2000);
            checkStatus();
            fetchStats();
        }
    } catch (error) {
        console.error('Error saving config:', error);
        btn.textContent = 'Error';
    }
}

async function checkStatus() {
    try {
        const response = await fetch(`${API_BASE}/status`);
        const data = await response.json();
        
        const openaiBadge = document.getElementById('openai-status');
        if (data.openai_api_key_configured) {
            openaiBadge.textContent = 'Configured';
            openaiBadge.className = 'badge success';
        } else {
            openaiBadge.textContent = 'Missing API Key';
            openaiBadge.className = 'badge error';
        }

        const dbBadge = document.getElementById('db-status');
        if (data.db_connected) {
            dbBadge.textContent = 'Connected';
            dbBadge.className = 'badge success';
        } else {
            dbBadge.textContent = 'Disconnected';
            dbBadge.className = 'badge error';
        }

        updatePipelineStatus(data.is_running);
        
        if (data.is_running && !logsInterval) {
            startLogPolling();
        }
    } catch (error) {
        console.error('Error checking status:', error);
        document.getElementById('status-indicator').style.backgroundColor = 'var(--error)';
    }
}

async function fetchStats() {
    try {
        const response = await fetch(`${API_BASE}/database/stats`);
        const data = await response.json();
        
        document.getElementById('total-chunks').textContent = data.total_chunks || 0;
        document.getElementById('total-skills').textContent = data.total_skills || 0;
    } catch (error) {
        console.error('Error fetching stats:', error);
    }
}

async function fetchSkills() {
    try {
        const response = await fetch(`${API_BASE}/skills`);
        const data = await response.json();
        const selector = document.getElementById('skill-selector');
        
        if (data.skills && data.skills.length > 0) {
            selector.innerHTML = data.skills.map(skill => 
                `<option value="${skill}">${skill}</option>`
            ).join('');
        } else {
            selector.innerHTML = '<option value="" disabled selected>No active skills found</option>';
        }
    } catch (error) {
        console.error('Error fetching skills:', error);
    }
}

async function assignSkillToAgent() {
    const agentName = document.getElementById('agent-selector').value;
    const skillName = document.getElementById('skill-selector').value;
    const msgDiv = document.getElementById('agent-msg');
    
    if (!skillName) {
        msgDiv.textContent = 'Please select a valid skill first.';
        msgDiv.style.color = 'var(--error)';
        return;
    }
    
    const btn = document.getElementById('assign-btn');
    btn.textContent = 'Binding...';
    btn.disabled = true;

    try {
        const response = await fetch(`${API_BASE}/agents/assign`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_name: agentName, skill_name: skillName })
        });
        const data = await response.json();
        
        if (data.status === 'success') {
            msgDiv.textContent = data.message;
            msgDiv.style.color = 'var(--success)';
        } else {
            msgDiv.textContent = data.message;
            msgDiv.style.color = 'var(--error)';
        }
    } catch (error) {
        console.error('Error assigning skill:', error);
        msgDiv.textContent = 'Failed to connect to agent.';
        msgDiv.style.color = 'var(--error)';
    } finally {
        btn.textContent = 'Bind Skill to Agent';
        btn.disabled = false;
        setTimeout(() => { msgDiv.textContent = ''; }, 5000);
    }
}

async function runPipeline() {
    const btn = document.getElementById('run-btn');
    btn.disabled = true;
    btn.innerHTML = `
        <svg class="animate-spin" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"></path></svg>
        Running...
    `;

    document.getElementById('terminal-output').innerHTML = '<div class="log-line text-muted">Starting orchestration pipeline...</div>';
    
    try {
        const response = await fetch(`${API_BASE}/run`, { method: 'POST' });
        const data = await response.json();
        
        if (data.status === 'success') {
            updatePipelineStatus(true);
            startLogPolling();
        } else {
            alert(data.message);
            resetRunButton();
        }
    } catch (error) {
        console.error('Error running pipeline:', error);
        resetRunButton();
    }
}

function updatePipelineStatus(isRunning) {
    const statusBadge = document.getElementById('pipeline-status');
    const indicator = document.getElementById('status-indicator');
    
    if (isRunning) {
        statusBadge.textContent = 'Running';
        statusBadge.className = 'badge active';
        indicator.style.animation = 'pulse 1s infinite';
        indicator.style.backgroundColor = '#6366f1';
    } else {
        statusBadge.textContent = 'Idle';
        statusBadge.className = 'badge';
        indicator.style.animation = 'pulse 2s infinite';
        indicator.style.backgroundColor = 'var(--success)';
        resetRunButton();
    }
}

function resetRunButton() {
    const btn = document.getElementById('run-btn');
    btn.disabled = false;
    btn.innerHTML = `
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
        Execute Pipeline
    `;
}

function startLogPolling() {
    if (logsInterval) clearInterval(logsInterval);
    
    logsInterval = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/logs`);
            const data = await response.json();
            
            const terminal = document.getElementById('terminal-output');
            if (data.logs) {
                const formattedLogs = data.logs.split('\n').map(line => {
                    if (line.includes('SUCCESS') || line.includes('ADD')) return `<div class="log-line" style="color: #4ade80">${line}</div>`;
                    if (line.includes('ERROR') || line.includes('DISCARD')) return `<div class="log-line" style="color: #f87171">${line}</div>`;
                    if (line.startsWith('=')) return `<div class="log-line" style="color: #60a5fa">${line}</div>`;
                    return `<div class="log-line">${line}</div>`;
                }).join('');
                
                terminal.innerHTML = formattedLogs;
                terminal.scrollTop = terminal.scrollHeight;
            }
            
            if (!data.is_running) {
                clearInterval(logsInterval);
                logsInterval = null;
                updatePipelineStatus(false);
                fetchStats();
            }
        } catch (error) {
            console.error('Error fetching logs:', error);
        }
    }, 1000);
}
