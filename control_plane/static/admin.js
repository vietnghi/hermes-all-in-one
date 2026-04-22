const initialStatus = JSON.parse(document.getElementById('initial-status').textContent);
let latestStatus = initialStatus;
let refreshTimer = null;

function qs(selector) {
  return document.querySelector(selector);
}

function qsa(selector) {
  return Array.from(document.querySelectorAll(selector));
}

function setPanel(panel) {
  qsa('.nav-link').forEach((button) => button.classList.toggle('active', button.dataset.panel === panel));
  qsa('.panel').forEach((section) => section.classList.toggle('active', section.id === `panel-${panel}`));
}

function renderStatus(status) {
  latestStatus = status;
  qs('#webui-status-line').textContent = status.webui.healthy
    ? `Healthy on ${status.webui.internal_base_url}`
    : 'Waiting for internal WebUI';
  qs('#gateway-status-line').textContent = status.gateway.running
    ? `Running · ${status.gateway.healthy ? 'healthy' : 'warming up'}`
    : `Stopped · autostart ${status.autostart ? 'eligible' : 'not ready yet'}`;
  qs('#paths-line').textContent = `${status.paths.hermes_home} · ${status.paths.workspace_dir}`;
  qs('#gateway-badge').textContent = status.gateway.running ? 'Running' : 'Stopped';
  qs('#gateway-log-box').textContent = status.gateway.log_tail.join('\n') || 'No gateway logs yet.';
  qs('#webui-log-box').textContent = status.webui.log_tail.join('\n') || 'No WebUI logs yet.';
}

async function refreshStatus() {
  const response = await fetch('/admin/api/status', { headers: { 'Accept': 'application/json' } });
  if (!response.ok) throw new Error(`Status refresh failed (${response.status})`);
  const payload = await response.json();
  renderStatus(payload);
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  if (payload.status) renderStatus(payload.status);
  if (payload.status && payload.status.webui && payload.status.gateway) renderStatus(payload.status);
  if (payload.status && !payload.status.webui) await refreshStatus();
  return payload;
}

function wireNavigation() {
  qsa('.nav-link').forEach((button) => {
    button.addEventListener('click', () => setPanel(button.dataset.panel));
  });
}

function wireRuntimeControls() {
  qsa('[data-gateway-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await postJson(`/admin/api/gateway/${button.dataset.gatewayAction}`, {});
        await refreshStatus();
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
      }
    });
  });

  qs('#restart-webui').addEventListener('click', async () => {
    try {
      await postJson('/admin/api/webui/restart', {});
      await refreshStatus();
    } catch (error) {
      alert(error.message);
    }
  });

  qs('#refresh-status').addEventListener('click', async () => {
    try {
      await refreshStatus();
    } catch (error) {
      alert(error.message);
    }
  });
}

function wireProviderForm() {
  const form = qs('#provider-form');
  const statusBox = qs('#provider-form-status');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    statusBox.textContent = 'Saving provider setup…';
    try {
      const payload = await postJson('/admin/api/provider/setup', data);
      statusBox.textContent = `Saved ${payload.result.provider} → ${payload.result.model}`;
      form.reset();
      await refreshStatus();
    } catch (error) {
      statusBox.textContent = error.message;
    }
  });
}

function wireChannelForm() {
  const form = qs('#channel-form');
  const statusBox = qs('#channel-form-status');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    statusBox.textContent = 'Saving channel settings…';
    try {
      await postJson('/admin/api/channels/save', data);
      statusBox.textContent = 'Channel settings saved.';
      form.reset();
      await refreshStatus();
    } catch (error) {
      statusBox.textContent = error.message;
    }
  });
}

function startPolling() {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    refreshStatus().catch(() => {});
  }, 5000);
}

wireNavigation();
wireRuntimeControls();
wireProviderForm();
wireChannelForm();
renderStatus(initialStatus);
startPolling();
