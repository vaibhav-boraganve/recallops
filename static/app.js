const $ = (sel) => document.querySelector(sel);
let currentAlert = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 5000);
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function renderMarkdown(md) {
  const lines = escapeHtml(md).split("\n");
  let html = "";
  let list = null;
  const inline = (s) => s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const line = raw.trim();
    let m;
    if ((m = line.match(/^#{1,4}\s+(.*)/))) { closeList(); html += `<h4>${inline(m[1])}</h4>`; }
    else if ((m = line.match(/^\d+[.)]\s+(.*)/))) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = line.match(/^[-*]\s+(.*)/))) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
    else if (line) { closeList(); html += `<p>${inline(line)}</p>`; }
  }
  closeList();
  return html;
}

function memoryItem(hit) {
  const when = hit.occurred_at ? new Date(hit.occurred_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
  const tags = (hit.tags || []).join(" · ");
  return `<li><span class="mmeta">${escapeHtml(hit.type)} · ${escapeHtml(when)} ${tags ? "· " + escapeHtml(tags) : ""}</span>${escapeHtml(hit.text)}</li>`;
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("#status").innerHTML = `
      <span class="pill">Memory: <b>${s.memory_backend === "hindsight" ? "Hindsight" : "local fallback"}</b></span>
      <span class="pill">Bank: <b>${escapeHtml(s.bank_id)}</b></span>
      <span class="pill ${s.llm_model ? "" : "off"}">LLM: <b>${escapeHtml(s.llm_model || "not configured")}</b></span>`;
  } catch (e) {
    $("#status").innerHTML = `<span class="pill off">Server unreachable</span>`;
  }
}

async function loadAlerts() {
  const alerts = await api("/api/alerts");
  $("#alerts").innerHTML = "";
  for (const alert of alerts) {
    const el = document.createElement("div");
    el.className = "card alert-item";
    el.innerHTML = `<div class="meta"><span class="sev ${alert.severity}">${alert.severity}</span>${escapeHtml(alert.id)} · ${escapeHtml(alert.service)}</div><div class="title">${escapeHtml(alert.title)}</div>`;
    el.onclick = () => {
      document.querySelectorAll(".alert-item").forEach((n) => n.classList.remove("active"));
      el.classList.add("active");
      triage(alert);
    };
    $("#alerts").appendChild(el);
  }
}

async function triage(alert) {
  currentAlert = alert;
  $("#empty").hidden = true;
  $("#triage").hidden = false;
  $("#t-sev").textContent = alert.severity;
  $("#t-sev").className = `sev ${alert.severity}`;
  $("#t-title").textContent = alert.title;
  $("#t-details").textContent = alert.details || "(no details)";
  $("#b-without").innerHTML = `<p class="loading">Thinking without memory…</p>`;
  $("#b-with").innerHTML = `<p class="loading">Recalling past incidents from Hindsight…</p>`;
  $("#cited").innerHTML = "";
  $("#recalled").innerHTML = "";
  $("#recalled-count").textContent = "…";
  $("#insight").hidden = true;

  try {
    const { without_memory, with_memory } = await api("/api/compare", { method: "POST", body: alert });
    $("#b-without").innerHTML = renderMarkdown(without_memory.brief);
    $("#b-with").innerHTML = renderMarkdown(with_memory.brief);
    $("#cited").innerHTML = with_memory.cited_incidents.map((id) => `<span>recalled ${escapeHtml(id)}</span>`).join("");
    $("#recalled-count").textContent = with_memory.recalled.length;
    $("#recalled").innerHTML = with_memory.recalled.map(memoryItem).join("") || `<li class="muted">Nothing similar in team memory yet.</li>`;
    if (with_memory.insight) {
      $("#insight").hidden = false;
      $("#insight").textContent = "Hindsight reflect · " + with_memory.insight;
    }
    if (with_memory.memory_error) toast(with_memory.memory_error, true);
  } catch (e) {
    $("#b-with").innerHTML = $("#b-without").innerHTML = `<p>Triage failed: ${escapeHtml(e.message)}</p>`;
  }
}

function lines(text) {
  return (text || "").split("\n").map((s) => s.trim()).filter(Boolean);
}

async function withBusy(button, fn) {
  button.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { button.disabled = false; }
}

$("#seed-btn").onclick = (ev) => withBusy(ev.target, async () => {
  toast("Retaining incident history into Hindsight… (Hindsight extracts facts with an LLM, this can take a minute)");
  const r = await api("/api/demo/seed", { method: "POST" });
  toast(`Retained ${r.retained} past incidents into team memory.`);
});

$("#prefill-btn").onclick = async () => {
  const inc = await api("/api/demo/new-resolution");
  const form = $("#resolution");
  for (const key of ["id", "service", "severity", "minutes_to_resolve", "title", "symptoms", "root_cause", "responder"]) {
    form.elements[key].value = inc[key] ?? "";
  }
  form.elements.fix_steps.value = inc.fix_steps.join("\n");
  form.elements.did_not_work.value = inc.did_not_work.join("\n");
  $("#resolution-box").open = true;
  $("#resolution-box").scrollIntoView({ behavior: "smooth" });
};

$("#resolution").onsubmit = (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  withBusy(ev.submitter, async () => {
    await api("/api/resolutions", {
      method: "POST",
      body: {
        id: f.id.value.trim(), service: f.service.value.trim(), severity: f.severity.value,
        minutes_to_resolve: Number(f.minutes_to_resolve.value || 0), title: f.title.value.trim(),
        symptoms: f.symptoms.value.trim(), root_cause: f.root_cause.value.trim(),
        fix_steps: lines(f.fix_steps.value), did_not_work: lines(f.did_not_work.value),
        responder: f.responder.value.trim() || "on-call",
      },
    });
    toast(`${f.id.value} retained. RecallOps will recall it next time.`);
    ev.target.reset();
  });
};

document.querySelectorAll(".feedback button").forEach((btn) => {
  btn.onclick = () => withBusy(btn, async () => {
    if (!currentAlert) return;
    await api("/api/feedback", {
      method: "POST",
      body: { alert: currentAlert, helpful: btn.dataset.helpful === "true", notes: $("#fb-notes").value },
    });
    $("#fb-notes").value = "";
    toast("Feedback retained. Future briefs will take it into account.");
  });
});

$("#custom-alert").onsubmit = (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  triage({
    id: `ALERT-${Date.now() % 100000}`, title: f.title.value.trim(), service: f.service.value.trim(),
    severity: f.severity.value, details: f.details.value,
  });
};

$("#search").onsubmit = (ev) => {
  ev.preventDefault();
  withBusy(ev.submitter, async () => {
    const hits = await api(`/api/memories?q=${encodeURIComponent(ev.target.elements.q.value)}`);
    $("#search-results").innerHTML = hits.map(memoryItem).join("") || `<li>No memories matched.</li>`;
  });
};

loadStatus();
loadAlerts();
