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
    document.getElementById("limit").textContent = s.limit_gb;
    document.getElementById("days").textContent = s.days_left;
    document.getElementById("online").textContent = s.is_online ? "●" : "○";
    document.getElementById("bar").style.width = s.percent + "%";
    const badge = document.getElementById("badge");
    badge.textContent = s.is_active ? "Active" : (s.reason || "Off");
    badge.className = "badge " + (s.is_active ? "on" : "off");
    document.getElementById("btn-toggle").textContent = s.is_active ? "Disable" : "Enable";
    const line = document.getElementById("addr-line");
    if (line && s.addr) line.textContent = s.addr;
  } catch (_) {}
}

async function loadLinks() {
  const data = await api("/api/links");
  const box = document.getElementById("links");
  box.innerHTML = "";
  document.getElementById("feed-box").style.display = "block";
  document.getElementById("feed-url").value = data.feed;
  const line = document.getElementById("addr-line");
  if (line && data.addr) line.textContent = data.addr + (data.sni ? " · " + data.sni : "");
  data.links.forEach(c => {
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

async function resetUsage() {
  if (!confirm("Reset usage counters?")) return;
  await api("/api/reset", { method: "POST" });
  refreshStatus();
}

async function restartAll() {
  const r = await api("/api/restart", { method: "POST" });
  alert("core=" + r.core + " gate=" + r.gate + (r.addr ? " addr=" + r.addr : ""));
  refreshStatus();
  loadLinks();
}

async function saveSettings(e) {
  e.preventDefault();
  const limit = parseFloat(document.getElementById("limit_in").value);
  const days = parseInt(document.getElementById("days_in").value);
  await api("/api/update", {
    method: "POST",
    body: JSON.stringify({ limit_gb: limit, expire_days: days })
  });
  refreshStatus();
  return false;
}

setInterval(refreshStatus, 30000);
loadLinks();
