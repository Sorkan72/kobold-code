const $ = (id) => document.getElementById(id);
let transcript = [];
let busy = false;
let activeController = null;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function markdownLite(text) {
  let s = escapeHtml(text);
  s = s.replace(/```([\w+-]*)\n([\s\S]*?)```/g, (_, lang, code) => `<pre><code>${code}</code></pre>`);
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  s = s.split(/\n{2,}/).map(p => p.startsWith("<pre>") ? p : `<p>${p.replace(/\n/g,"<br>")}</p>`).join("");
  return s;
}
function addMessage(role, text, live=false) {
  $("welcome").classList.add("hidden");
  $("conversation").classList.remove("hidden");
  const el = document.createElement("div");
  el.className = `message ${role}`;
  el.innerHTML = `<div class="message-role">${role === "user" ? "You" : "KoboldCode"}</div><div class="message-body">${live ? "" : markdownLite(text)}</div>`;
  $("conversation").appendChild(el);
  $("conversation").scrollTop = $("conversation").scrollHeight;
  return el.querySelector(".message-body");
}
function logActivity(name, details) {
  const panel = $("activity");
  panel.classList.remove("hidden");
  if (!panel.dataset.started) {
    panel.dataset.started = "1";
    panel.innerHTML = '<div class="activity-title">AGENT ACTIVITY</div><div id="activity-items"></div>';
  }
  const item = document.createElement("div");
  item.className = "activity-item";
  item.innerHTML = `<span class="activity-name">${escapeHtml(name)}</span> ${escapeHtml(details || "")}`;
  $("activity-items").appendChild(item);
  panel.scrollTop = panel.scrollHeight;
}
function setBusy(value) {
  busy = value;
  $("send").disabled = value;
  $("send").innerHTML = value ? 'Working <span>…</span>' : 'Run task <span>↑</span>';
}
function showApproval(data) {
  return new Promise(resolve => {
    const host = $("approval-host");
    host.innerHTML = "";
    const card = document.createElement("div");
    card.className = "approval-card";
    card.innerHTML = `<div class="approval-title">Approval required · Shell command</div><div class="approval-command">${escapeHtml(data.args?.command || JSON.stringify(data.args,null,2))}</div><div class="approval-actions"><button class="deny">Deny</button><button class="approve">Allow command</button></div>`;
    host.appendChild(card);
    const finish = async approved => {
      card.querySelectorAll("button").forEach(b => b.disabled = true);
      try { await fetch(`/api/approve/${encodeURIComponent(data.id)}`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({approved})}); } catch {}
      card.remove();
      resolve(approved);
    };
    card.querySelector(".approve").addEventListener("click", () => finish(true));
    card.querySelector(".deny").addEventListener("click", () => finish(false));
  });
}
async function sendMessage(text) {
  if (busy || !text.trim()) return;
  setBusy(true);
  $("activity").classList.add("hidden");
  $("activity").dataset.started = "";
  $("activity").innerHTML = "";
  $("approval-host").innerHTML = "";
  addMessage("user", text);
  const assistantBody = addMessage("assistant", "", true);
  let currentText = "";
  const payload = {messages: transcript, user_message: text};
  $("prompt").value = "";
  $("prompt").style.height = "auto";
  activeController = new AbortController();
  try {
    const res = await fetch("/api/chat", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),signal:activeController.signal});
    if (!res.ok) throw new Error(await res.text());
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finishedMessages = null;
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value,{stream:true});
      let split;
      while ((split = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0,split); buffer = buffer.slice(split+2);
        let event = "message", data = "";
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        let obj; try { obj = JSON.parse(data); } catch { continue; }
        if (event === "token") {
          currentText += obj.text || "";
          assistantBody.innerHTML = markdownLite(currentText);
          $("conversation").scrollTop = $("conversation").scrollHeight;
        } else if (event === "status") {
          logActivity("STATUS", obj.text);
        } else if (event === "tool_start") {
          logActivity("TOOL", `${obj.name} · ${JSON.stringify(obj.args || {})}`);
        } else if (event === "tool_result") {
          let detail = JSON.stringify(obj.result || {});
          logActivity("RESULT", `${obj.name} · ${detail.slice(0,500)}`);
        } else if (event === "approval_required") {
          logActivity("APPROVAL", "Waiting for your decision");
          await showApproval(obj);
        } else if (event === "error") {
          currentText += (currentText ? "\n\n" : "") + `Error: ${obj.message}`;
          assistantBody.innerHTML = markdownLite(currentText);
        } else if (event === "done") {
          finishedMessages = obj.messages;
          if (!currentText && obj.message) currentText = obj.message;
          assistantBody.innerHTML = markdownLite(currentText || "Task completed.");
        }
      }
    }
    if (finishedMessages) {
      transcript = finishedMessages.filter(m => ["user","assistant","tool"].includes(m.role)).slice(-40);
    } else {
      transcript.push({role:"user",content:text});
      if (currentText) transcript.push({role:"assistant",content:currentText});
      transcript = transcript.slice(-40);
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      currentText += (currentText ? "\n\n" : "") + `Connection error: ${e.message}`;
      assistantBody.innerHTML = markdownLite(currentText);
    }
  } finally {
    setBusy(false);
    activeController = null;
    $("conversation").scrollTop = $("conversation").scrollHeight;
    refreshWorkspace();
  }
}
$("composer").addEventListener("submit", e => { e.preventDefault(); sendMessage($("prompt").value); });
$("prompt").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
$("prompt").addEventListener("input", () => { $("prompt").style.height = "auto"; $("prompt").style.height = Math.min($("prompt").scrollHeight,180) + "px"; });
$("new-chat").addEventListener("click", () => {
  if (busy) return;
  transcript = [];
  $("conversation").innerHTML = "";
  $("conversation").classList.add("hidden");
  $("activity").classList.add("hidden");
  $("approval-host").innerHTML = "";
  $("welcome").classList.remove("hidden");
});
document.querySelectorAll(".suggestion").forEach(b => b.addEventListener("click", () => { $("prompt").value = b.dataset.prompt; $("prompt").focus(); }));
$("refresh-files").addEventListener("click", refreshWorkspace);
$("open-settings").addEventListener("click", openSettings);
$("open-diagnostics").addEventListener("click", openDiagnostics);
document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => $(b.dataset.close + "-modal").classList.add("hidden")));
$("rerun-diagnostics").addEventListener("click", loadDiagnostics);

async function refreshWorkspace() {
  try {
    const r = await fetch("/api/workspace");
    const data = await r.json();
    $("workspace-path").textContent = data.workspace;
    $("crumb-workspace").textContent = data.workspace.split(/[\\/]/).filter(Boolean).pop() || "Workspace";
    $("file-tree").innerHTML = "";
    for (const item of data.entries || []) {
      const el = document.createElement("div");
      el.className = "tree-item";
      el.title = item.path;
      el.innerHTML = `<span class="tree-icon">${item.is_dir ? "▸" : "·"}</span><span>${escapeHtml(item.name)}</span>`;
      el.addEventListener("click", () => {
        if (!item.is_dir) openFile(item.path);
      });
      $("file-tree").appendChild(el);
    }
  } catch {}
}
async function openFile(path) {
  try {
    const r = await fetch("/api/files?path=" + encodeURIComponent(path));
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Could not read file");
    if (data.is_dir) return;
    const body = addMessage("assistant", `File preview: ${path}\n\n\`\`\`\n${data.content}\n\`\`\``);
    body.innerHTML = markdownLite(`File preview: ${path}\n\n\`\`\`\n${data.content}\n\`\`\``);
  } catch (e) { alert(e.message); }
}
async function openSettings() {
  const r = await fetch("/api/settings"); const s = await r.json();
  $("setting-url").value = s.base_url || "";
  $("setting-key").value = s.api_key || "";
  $("setting-workspace").value = s.workspace || "";
  $("setting-model").value = s.model || "";
  $("setting-tokens").value = s.max_tokens || 4096;
  $("setting-temperature").value = s.temperature ?? 0.2;
  $("setting-approval").checked = s.require_shell_approval !== false;
  $("setting-rounds").value = s.max_tool_rounds || 12;
  $("settings-status").textContent = "";
  $("settings-modal").classList.remove("hidden");
}
$("settings-form").addEventListener("submit", async e => {
  e.preventDefault();
  const data = {
    base_url:$("setting-url").value.trim(),
    api_key:$("setting-key").value,
    workspace:$("setting-workspace").value.trim(),
    model:$("setting-model").value.trim(),
    max_tokens:Number($("setting-tokens").value),
    temperature:Number($("setting-temperature").value),
    require_shell_approval:$("setting-approval").checked,
    max_tool_rounds:Number($("setting-rounds").value)
  };
  const status = $("settings-status");
  status.textContent = "Saving…";
  try {
    const r = await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
    const out = await r.json();
    if (!r.ok) throw new Error(out.detail || "Unable to save settings");
    status.textContent = "Saved";
    $("settings-modal").classList.add("hidden");
    await refreshWorkspace(); await refreshConnection();
  } catch (e) { status.textContent = e.message; }
});
async function refreshConnection() {
  try {
    const r = await fetch("/api/diagnostics"); const d = await r.json();
    const pill = $("connection-pill");
    pill.className = "connection-pill " + (d.reachable ? "online" : "offline");
    pill.innerHTML = `<span class="dot"></span><span>${d.reachable ? "KoboldCpp connected" : "Backend unavailable"}</span>`;
    const modelData = d.model?.data?.data || d.model?.data?.models || [];
    const name = Array.isArray(modelData) && modelData.length ? (modelData[0].id || modelData[0].name || "Model detected") : "KoboldCpp";
    $("model-chip").textContent = d.reachable ? name : "Not connected";
  } catch {}
}
async function openDiagnostics() {
  $("diagnostics-modal").classList.remove("hidden");
  await loadDiagnostics();
}
async function loadDiagnostics() {
  $("diagnostics-body").innerHTML = '<div class="loading-line">Checking endpoints…</div>';
  try {
    const r = await fetch("/api/diagnostics"); const d = await r.json();
    const esc = escapeHtml;
    let html = `<div class="diagnostic-item"><div class="diagnostic-head"><b>Connection</b><span class="${d.reachable ? "status-ok" : "status-bad"}">${d.reachable ? "Reachable" : "Unavailable"}</span></div><div class="diagnostic-path">${esc(d.base_url)}</div></div>`;
    for (const c of d.checks || []) {
      const ok = c.status >= 200 && c.status < 300;
      html += `<div class="diagnostic-item"><div class="diagnostic-head"><b>${esc(c.name)}</b><span class="${ok ? "status-ok" : "status-bad"}">${c.status ? "HTTP " + c.status : "Failed"}</span></div><div class="diagnostic-path">${esc(c.path)}</div><div class="diagnostic-data">${esc(JSON.stringify(c.data ?? c.error ?? {},null,2).slice(0,4500))}</div></div>`;
    }
    $("diagnostics-body").innerHTML = html;
  } catch (e) { $("diagnostics-body").innerHTML = `<div class="diagnostic-item status-bad">${escapeHtml(e.message)}</div>`; }
}
refreshWorkspace();
refreshConnection();
setInterval(refreshConnection, 20000);
