async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts
  });
  return r.json();
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    document.getElementById("used").textContent = s.used_gb;
    document.getElementById("quota").textContent = s.quota_gb;
    document.getElementById("days").textContent = s.days_left;
    document.getElementById("online").textContent = s.is_online ? "●" : "○";
    document.getElementById("bar").style.width = s.percent + "%";
    const badge = document.getElementById("badge");
    badge.textContent = s.is_active ? "Active" : (s.reason || "Off");
    badge.className = "badge " + (s.is_active ? "on" : "off");
    document.getElementById("btn-toggle").textContent = s.is_active ? "Disable" : "Enable";
  } catch (_) {}
}

async function loadConfigs() {
  const data = await api("/api/configs");
  const box = document.getElementById("configs");
  box.innerHTML = "";
  document.getElementById("sub-box").style.display = "block";
  document.getElementById("sub-url").value = data.sub;

  data.configs.forEach(c => {
    const div = document.createElement("div");
    div.className = "cfg";
    div.innerHTML = `
      <div class="cfg-title">${c.title} · ${c.tag}</div>
      <textarea readonly rows="2">${c.config}</textarea>
      <button class="btn-sm" style="margin-top:6px" onclick="navigator.clipboard.writeText(this.previousElementSibling.value)">Copy</button>
    `;
    box.appendChild(div);
  });
}

function copy(id) {
  const el = document.getElementById(id);
  navigator.clipboard.writeText(el.value);
}

async function toggle() {
  await api("/api/toggle", { method: "POST" });
  refreshStatus();
}

async function resetTraffic() {
  if (!confirm("Reset used traffic to zero?")) return;
  await api("/api/reset", { method: "POST" });
  refreshStatus();
}

async function updateSettings(e) {
  e.preventDefault();
  const quota = parseFloat(document.getElementById("quota_in").value);
  const days = parseInt(document.getElementById("days_in").value);
  await api("/api/update", {
    method: "POST",
    body: JSON.stringify({ quota_gb: quota, expire_days: days })
  });
  refreshStatus();
  return false;
}

// auto refresh every 30s
setInterval(refreshStatus, 30000);
loadConfigs();
