/* SPDX-License-Identifier: GPL-3.0-or-later */
/* LoRa Bridge dashboard — vanilla JS */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const csrf = () => sessionStorage.getItem("csrf") || "";

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf() };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { location.reload(); throw new Error("unauthorized"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ---------------- session ---------------- */
async function initSession() {
  try {
    const s = await api("/api/session", { method: "GET" });
    if (s.authed) {
      sessionStorage.setItem("csrf", s.csrf || "");
      enterApp(s.user);
      return;
    }
  } catch (e) { /* not authed */ }
  $("login-view").classList.remove("hidden");
  $("app-view").classList.add("hidden");
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("login-btn"); btn.disabled = true;
  $("login-error").classList.add("hidden");
  try {
    const r = await api("/api/login", {
      method: "POST",
      body: { username: $("login-user").value, password: $("login-pass").value },
    });
    sessionStorage.setItem("csrf", r.csrf || "");
    enterApp(r.user);
  } catch (e) {
    $("login-error").textContent = e.message; $("login-error").classList.remove("hidden");
  } finally { btn.disabled = false; }
});

$("logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  sessionStorage.clear();
  location.reload();
});

function enterApp(user) {
  $("login-view").classList.add("hidden");
  $("app-view").classList.remove("hidden");
  $("user-label").textContent = user;
  refreshStatus();
  refreshBridge();
  refreshDevices();
  refreshAudit();
  setInterval(refreshStatus, 5000);
}

/* ---------------- tabs ---------------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((x) => x.classList.add("hidden"));
    t.classList.add("active");
    $("tab-" + t.dataset.tab).classList.remove("hidden");
    if (t.dataset.tab === "logs") refreshAudit();
    if (t.dataset.tab === "devices") { loadScanUi(); refreshDevices(); }
  });
});

// gentle background refresh of the discovered list while the tab is open
setInterval(() => {
  const tab = document.querySelector(".tab.active");
  if (tab && tab.dataset.tab === "devices") refreshDevices();
}, 30000);

/* ---------------- overview ---------------- */
async function refreshStatus() {
  try {
    const s = await api("/api/status", { method: "GET" });
    renderServices(s.services);
    $("gateway-info").textContent = JSON.stringify(s.gateway, null, 2);
    $("rns-info").textContent = JSON.stringify(s.rns, null, 2);
    renderRnsLeg(s.rns_leg);
    renderSerial(s.serial);
    renderFeed(s.events.events);
  } catch (e) { /* retry next tick */ }
  try {
    const t = await api("/api/peers/tracker", { method: "GET" });
    renderPeerOverview(t.config || {}, t.status || {});
  } catch (e) { /* tracker not present */ }
}

function renderPeerOverview(cfg, s) {
  const el = $("pt-overview");
  if (!el) return;
  const label = s.period_label || (cfg.period_value + " " + cfg.period_unit);
  $("pt-window").textContent = label;
  const dot = s.enabled ? "ok" : "missing";
  const due = s.notice_due ? "<span class='badge active'>notice due</span>" : "<span class='badge inactive'>notice queued per period</span>";
  el.innerHTML =
    `<div class="unit"><span class="dot ${dot}"></span> total unique <strong>${esc(s.total_unique ?? "?")}</strong></div>
     <div class="unit"><span class="dot ok"></span> unique last ${esc(label)} <strong>${esc(s.unique_last_period ?? "?")}</strong> ` +
    `(${esc(s.lxmf_last_period ?? "?")} LXMF / ${esc(s.rns_last_period ?? "?")} RNS)</div>
     <div class="unit">${due} <span class="muted small">notices throttled to once per ${esc(label)}</span></div>`;
}

function renderServices(svcs) {
  const order = ["mosquitto", "mesh-gateway", "mqtt-bridge", "rns-bridge", "rns-status-server", "rnsd"];
  $("svc-cards").innerHTML = order.map((n) => {
    const s = svcs[n] || {};
    const st = s.state || "inactive";
    return `<div class="svc-card">
      <div class="name">${esc(n)}</div>
      <span class="badge ${esc(st)}">${esc(st)}</span>
      <div class="meta">pid ${esc(s.pid || "—")}${s.uptime_since ? "<br>since " + esc(s.uptime_since.split(" ").slice(0, 2).join(" ")) : ""}</div>
    </div>`;
  }).join("");
  $("svc-restart-row").innerHTML = order.map((n) =>
    `<button data-unit="${esc(n)}" class="svc-restart ghost">restart ${esc(n)}</button>`).join("");
  document.querySelectorAll(".svc-restart").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`Restart ${b.dataset.unit}?`)) return;
      const r = await api("/api/services/restart", { method: "POST", body: { unit: b.dataset.unit } });
      showResult("svc-restart-row", JSON.stringify(r));
      refreshStatus();
    }));
}

function renderRnsLeg(leg) {
  const el = $("rns-leg");
  if (!leg || !leg.interfaces) { el.innerHTML = "<div class='muted small'>no leg data yet (bridge writes status every 30s)</div>"; return; }
  const rows = (leg.interfaces || []).map((i) =>
    `<div class="unit"><span class="dot ${i.online ? "ok" : "missing"}"></span>
     <code>${esc(i.name)}</code><span class="muted small">${esc(i.type)}</span>
     <span class="muted small">rx ${esc(i.rxb)} · tx ${esc(i.txb)}</span>
     <span class="${i.online ? "badge active" : "badge inactive"}">${i.online ? "online" : "offline"}</span></div>`).join("");
  el.innerHTML = `
    <div class="unit"><span class="dot ok"></span> destination <code>${esc(leg.destination_hash || "?")}</code></div>
    <div class="unit"><span class="dot ok"></span> RNS peers <strong>${esc(leg.peers)}</strong> · LXMF peers <strong>${esc(leg.lxmf_peers)}</strong></div>
    ${rows}`;
}

function renderSerial(units) {
  $("serial-units").innerHTML = (units || []).map((u) =>
    `<div class="unit"><span class="dot ${u.present ? "ok" : "missing"}"></span>
     <code>${esc(u.dev)}</code><strong>${esc(u.protocol)}</strong>
     <span class="muted small">${esc(u.note)}</span>
     <span class="muted small">${u.present ? "present" : "NOT present"}</span></div>`).join("") || "none";
}

function renderFeed(events) {
  const feed = $("event-feed");
  const html = (events || []).slice(-80).map((e) => {
    const net = e.net || "?";
    const txt = e.text || e.msg || JSON.stringify(e);
    return `<div class="ev"><span class="t">${esc((e.ts || e.time || "").toString().slice(11, 19))}</span>
      <span class="net net-${esc(net)}">${esc(net)}</span><span class="txt">${esc(txt)}</span></div>`;
  }).join("");
  if (feed.innerHTML !== html) { feed.innerHTML = html; feed.scrollTop = feed.scrollHeight; }
}

function showResult(id, text, ok = true) {
  const el = $(id); if (!el) return;
  el.textContent = text;
  el.style.color = ok ? "var(--ok)" : "var(--danger)";
  setTimeout(() => { el.textContent = ""; }, 12000);
}

/* ---------------- bridge ---------------- */
let brokers = [];

async function refreshBridge() {
  try {
    const m = await api("/api/bridge/mqtt", { method: "GET" });
    brokers = m.brokers || [];
    renderBrokers();
    const r = await api("/api/bridge/rns", { method: "GET" });
    $("rns-app").value = r.settings.announce_app;
    $("rns-name").value = r.settings.announce_name;
    $("rns-interval").value = r.settings.reannounce_interval_s;
    $("rns-announce").checked = r.settings.announce_enabled;
    $("rns-lxmf").checked = r.settings.lxmf_enabled;
    $("rns-identity").textContent = `identity: ${r.identity_file} (${r.identity_exists ? "exists — stable hash" : "missing, will be created"})`;
  } catch (e) { /* ignore */ }
  try {
    const t = await api("/api/peers/tracker", { method: "GET" });
    $("pt-unit").value = t.config.period_unit || "days";
    $("pt-value").value = t.config.period_value || 1;
    $("pt-enabled").checked = !!t.config.enabled;
    const s = t.status || {};
    $("pt-status").textContent =
      `total unique: ${s.total_unique ?? "?"} · unique last ${s.period_label ?? "?"}: ${s.unique_last_period ?? "?"} ` +
      `(${s.lxmf_last_period ?? "?"} LXMF / ${s.rns_last_period ?? "?"} RNS) · notice due: ${s.notice_due ? "YES" : "no"}`;
  } catch (e) { /* tracker not present */ }
}

function renderBrokers() {
  $("broker-list").innerHTML = brokers.map((b, i) => {
    const f = (name, val, ph, w) => `<input type="text" data-b="${i}" data-f="${name}" value="${esc(val)}" placeholder="${esc(ph)}" style="width:${w}">`;
    return `<div class="broker">
      <div class="broker-head"><strong>${esc(b.name || "broker")}</strong>
        <button class="del ghost" data-del="${i}">remove</button></div>
      <div class="broker-fields">
        ${f("name", b.name, "name", "100%")}
        ${f("host", b.host, "host", "100%")}
        ${f("port", b.port, "1883", "70px")}
        ${f("username", b.username, "user", "100%")}
        ${f("password", b.password || "", "password (unchanged if ****)", "100%")}
        ${f("mode", b.mode, "in|out|both", "80px")}
        ${f("publish_topic", b.publish_topic, "publish topic", "100%")}
        ${f("subscribe_topic", b.subscribe_topic, "subscribe topic", "100%")}
        ${f("status_topic", b.status_topic, "status topic", "100%")}
        <label class="check"><input type="checkbox" data-b="${i}" data-f="tls" ${b.tls ? "checked" : ""}> TLS</label>
        <label class="check"><input type="checkbox" data-b="${i}" data-f="status" ${b.status ? "checked" : ""}> enabled</label>
      </div></div>`;
  }).join("") || "<div class='muted small'>no brokers configured</div>";
  document.querySelectorAll("#broker-list input").forEach((inp) => {
    inp.addEventListener("change", () => {
      const b = brokers[+inp.dataset.b]; const f = inp.dataset.f;
      b[f] = inp.type === "checkbox" ? inp.checked : inp.value;
    });
  });
  document.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => { brokers.splice(+b.dataset.del, 1); renderBrokers(); }));
}

$("broker-add").addEventListener("click", () => {
  brokers.push({ name: "new-broker", host: "", port: 1883, username: "", password: "",
    tls: false, mode: "both", status: true, publish_topic: "meshcore/armaros",
    subscribe_topic: "meshcore/armaros", status_topic: "meshcore/status" });
  renderBrokers();
});

$("broker-save").addEventListener("click", async () => {
  const btn = $("broker-save"); btn.disabled = true;
  try {
    const r = await api("/api/bridge/mqtt", { method: "POST", body: { brokers } });
    showResult("broker-msg", r.restart && r.restart.ok ? "saved + mqtt-bridge restarted" : "saved; restart: " + JSON.stringify(r.restart), !!r.ok);
  } catch (e) { showResult("broker-msg", e.message, false); }
  finally { btn.disabled = false; }
});

$("rns-save").addEventListener("click", async () => {
  const btn = $("rns-save"); btn.disabled = true;
  try {
    const r = await api("/api/bridge/rns", {
      method: "POST",
      body: { settings: {
        announce_app: $("rns-app").value, announce_name: $("rns-name").value,
        reannounce_interval_s: +$("rns-interval").value,
        announce_enabled: $("rns-announce").checked, lxmf_enabled: $("rns-lxmf").checked } },
    });
    showResult("rns-msg", r.restart && r.restart.ok ? "saved + rns-bridge restarted" : "saved; restart: " + JSON.stringify(r.restart), !!r.ok);
  } catch (e) { showResult("rns-msg", e.message, false); }
  finally { btn.disabled = false; }
});

$("pt-save").addEventListener("click", async () => {
  const btn = $("pt-save"); btn.disabled = true;
  try {
    const r = await api("/api/peers/tracker", {
      method: "POST",
      body: { settings: {
        period_unit: $("pt-unit").value, period_value: +$("pt-value").value,
        enabled: $("pt-enabled").checked } },
    });
    showResult("pt-msg", r.ok ? "saved — tracker picks it up within ~10s (no restart needed)" : JSON.stringify(r), !!r.ok);
    refreshBridge();
  } catch (e) { showResult("pt-msg", e.message, false); }
  finally { btn.disabled = false; }
});

/* ---------------- protocols ---------------- */
function fillFrom(obj, idmap) {
  for (const [k, id] of Object.entries(idmap)) {
    const el = $(id);
    if (el && obj[k] !== undefined && obj[k] !== null) el.value = obj[k];
  }
}

/* MeshCore */
$("mc-connect").addEventListener("click", async () => {
  const btn = $("mc-connect"); btn.disabled = true;
  try {
    const r = await api("/api/protocols/meshcore/read", {
      method: "POST", body: { host: $("mc-host").value, port: +$("mc-port").value } });
    if (r.error) { $("mc-out").textContent = "ERROR: " + r.error; return; }
    $("mc-out").textContent = JSON.stringify(r, null, 2);
    const t = r.tuning || {}; const te = r.self_telemetry || {};
    $("mc-form").style.display = "grid";
    $("mc-actions").style.display = "flex";
    fillFrom(t, { frequency: "mc-freq", bandwidth: "mc-bw", spread_factor: "mc-sf", coding_rate: "mc-cr" });
    if (te.tx_power !== undefined) $("mc-txp").value = te.tx_power;
    if (te.name !== undefined) $("mc-name").value = te.name;
  } catch (e) { $("mc-out").textContent = "ERROR: " + e.message; }
  finally { btn.disabled = false; }
});

$("mc-apply").addEventListener("click", async () => {
  const r = await api("/api/protocols/meshcore/apply", {
    method: "POST",
    body: { host: $("mc-host").value, port: +$("mc-port").value,
      fields: { frequency: $("mc-freq").value, bandwidth: $("mc-bw").value,
        spread_factor: $("mc-sf").value, coding_rate: $("mc-cr").value,
        tx_power: $("mc-txp").value, name: $("mc-name").value } } });
  showResult("mc-msg", JSON.stringify(r), !r.error);
});
$("mc-advert").addEventListener("click", async () => {
  const r = await api("/api/protocols/meshcore/action", {
    method: "POST", body: { host: $("mc-host").value, port: +$("mc-port").value, action: "advert" } });
  showResult("mc-msg", JSON.stringify(r), !r.error);
});
$("mc-reboot").addEventListener("click", async () => {
  if (!confirm("Reboot the MeshCore device?")) return;
  const r = await api("/api/protocols/meshcore/action", {
    method: "POST", body: { host: $("mc-host").value, port: +$("mc-port").value, action: "reboot" } });
  showResult("mc-msg", JSON.stringify(r), !r.error);
});

/* Meshtastic */
$("mt-connect").addEventListener("click", async () => {
  const btn = $("mt-connect"); btn.disabled = true;
  try {
    const r = await api("/api/protocols/meshtastic/read", { method: "POST", body: { host: $("mt-host").value } });
    if (r.error) { $("mt-out").textContent = "ERROR: " + r.error; return; }
    $("mt-out").textContent = JSON.stringify(r, null, 2);
    $("mt-form").style.display = "grid";
    $("mt-actions").style.display = "flex";
    fillFrom(r.lora, { region: "mt-region", modem_preset: "mt-preset", frequency: "mt-freq",
      bandwidth: "mt-bw", spread_factor: "mt-sf", coding_rate: "mt-cr", tx_power: "mt-txp" });
    if (r.mqtt) $("mt-mqtt-root").value = r.mqtt.root || "";
  } catch (e) { $("mt-out").textContent = "ERROR: " + e.message; }
  finally { btn.disabled = false; }
});
$("mt-apply").addEventListener("click", async () => {
  const r = await api("/api/protocols/meshtastic/apply", {
    method: "POST",
    body: { host: $("mt-host").value,
      fields: { region: $("mt-region").value, modem_preset: $("mt-preset").value,
        frequency: $("mt-freq").value, bandwidth: $("mt-bw").value,
        spread_factor: $("mt-sf").value, coding_rate: $("mt-cr").value,
        tx_power: $("mt-txp").value, mqtt_root: $("mt-mqtt-root").value } } });
  showResult("mt-msg", JSON.stringify(r), !r.error);
});
$("mt-reboot").addEventListener("click", async () => {
  if (!confirm("Reboot the Meshtastic device?")) return;
  const r = await api("/api/protocols/meshtastic/action", {
    method: "POST", body: { host: $("mt-host").value, action: "reboot" } });
  showResult("mt-msg", JSON.stringify(r), !r.error);
});

/* RNode */
$("rn-connect").addEventListener("click", async () => {
  const btn = $("rn-connect"); btn.disabled = true;
  try {
    const r = await api("/api/protocols/rnode/read", { method: "POST", body: { port: $("rn-port").value } });
    if (r.error) { $("rn-out").textContent = "ERROR: " + r.error; return; }
    $("rn-out").textContent = JSON.stringify(r, null, 2);
    $("rn-form").style.display = "grid";
    fillFrom(r, { frequency: "rn-freq", bandwidth: "rn-bw", spread_factor: "rn-sf",
      coding_rate: "rn-cr", tx_power: "rn-txp" });
  } catch (e) { $("rn-out").textContent = "ERROR: " + e.message; }
  finally { btn.disabled = false; }
});
$("rn-apply").addEventListener("click", async () => {
  if (!confirm("Write EEPROM? rnsd will stop for a few seconds.")) return;
  const btn = $("rn-apply"); btn.disabled = true;
  try {
    const r = await api("/api/protocols/rnode/apply", {
      method: "POST",
      body: { port: $("rn-port").value, restart_rnsd: $("rn-restart").checked,
        fields: { frequency: $("rn-freq").value, bandwidth: $("rn-bw").value,
          spread_factor: $("rn-sf").value, coding_rate: $("rn-cr").value,
          tx_power: $("rn-txp").value } } });
    showResult("rn-msg", JSON.stringify(r, null, 2), !!r.ok);
  } catch (e) { showResult("rn-msg", e.message, false); }
  finally { btn.disabled = false; }
});

/* ---------------- devices ---------------- */
let savedDevices = [];
let devRefreshTimer = null;

async function refreshDevices() {
  try {
    const r = await api("/api/devices", { method: "GET" });
    savedDevices = r.devices || [];
    renderSaved();
    renderDiscovered(r.discovered || []);
  } catch (e) { /* ignore */ }
}

async function loadScanUi() {
  try {
    const sc = await api("/api/scan/config", { method: "GET" });
    $("sc-lan").checked = !!sc.lan;
    $("sc-tailnet").checked = !!sc.tailnet;
    $("sc-adv").checked = !!sc.advertised;
    $("sc-custom").value = sc.custom_ranges || "";
    $("sc-p5000").checked = (sc.ports || []).includes(5000);
    $("sc-p4403").checked = (sc.ports || []).includes(4403);
    $("sc-timeout").value = sc.timeout_ms;
    $("sc-auto").value = sc.auto_interval_min;
  } catch (e) { /* ignore */ }
}

function scanOverrides() {
  const ports = [];
  if ($("sc-p5000").checked) ports.push(5000);
  if ($("sc-p4403").checked) ports.push(4403);
  return {
    lan: $("sc-lan").checked,
    tailnet: $("sc-tailnet").checked,
    advertised: $("sc-adv").checked,
    custom_ranges: $("sc-custom").value,
    ports,
    timeout_ms: +$("sc-timeout").value,
  };
}

$("sc-save").addEventListener("click", async () => {
  const r = await api("/api/scan/config", { method: "POST", body: { ...scanOverrides(), auto_interval_min: +$("sc-auto").value } });
  $("dev-scan-info").textContent = `settings saved (auto-scan ${r.auto_interval_min > 0 ? "every " + r.auto_interval_min + " min" : "off"})`;
});

$("dev-scan").addEventListener("click", async () => {
  const btn = $("dev-scan"); btn.disabled = true;
  $("dev-scan-info").textContent = "scanning…";
  try {
    const r = await api("/api/devices/scan", { method: "POST", body: scanOverrides() });
    const err = (r.errors || []).length ? " · errors: " + r.errors.join("; ") : "";
    $("dev-scan-info").textContent =
      `scanned ${r.scanned} hosts (lan ${r.sources.lan || 0} / tailnet ${r.sources.tailnet || 0}` +
      `${r.sources.advertised ? " / adv " + r.sources.advertised : ""} / custom ${r.sources.custom || 0}) ` +
      `in ${r.duration_s}s · ${r.found.length} open service port(s)${err}`;
    refreshDevices();
  } catch (e) { $("dev-scan-info").textContent = "scan failed: " + e.message; }
  finally { btn.disabled = false; }
});

function renderDiscovered(list) {
  const el = $("dev-discovered");
  if (!list.length) { el.innerHTML = "<div class='muted small'>nothing discovered yet — run a scan</div>"; return; }
  el.innerHTML = list.map((d) => {
    const ago = Math.round((Date.now() / 1000 - d.last_seen) / 60);
    const badge = d.alive ? "<span class='badge active'>alive</span>" : `<span class='badge inactive'>${ago} min ago</span>`;
    const ports = (d.ports || []).map((p) => `:${p}`).join(" ");
    const vmc = (d.ports || []).includes(5000) ? `<button class="ghost" data-vmc="${esc(d.ip)}">verify MeshCore</button>` : "";
    const vmt = (d.ports || []).includes(4403) ? `<button class="ghost" data-vmt="${esc(d.ip)}">verify Meshtastic</button>` : "";
    const save = savedDevices.some((s) => s.host === d.ip) ? "" : `<button class="ghost" data-save="${esc(d.ip)}">+ save</button>`;
    return `<div class="unit"><span class="dot ${d.alive ? "ok" : "missing"}"></span>
      <code>${esc(d.ip)}</code>
      ${d.is_new ? "<span class='badge warn'>NEW</span>" : ""}
      <span class="muted small">${esc(d.name || "")}</span>
      <span class="muted small">${esc(ports)} · ${esc((d.services || []).join(", "))}</span>
      ${badge} ${vmc} ${vmt} ${save}
      <button class="ghost danger" data-forget="${esc(d.ip)}">forget</button></div>`;
  }).join("");
  el.querySelectorAll("[data-vmc]").forEach((b) =>
    b.addEventListener("click", () => verifyDevice("meshcore", b.dataset.vmc)));
  el.querySelectorAll("[data-vmt]").forEach((b) =>
    b.addEventListener("click", () => verifyDevice("meshtastic", b.dataset.vmt)));
  el.querySelectorAll("[data-save]").forEach((b) => {
    b.addEventListener("click", () => {
      savedDevices.push({ name: b.dataset.save, host: b.dataset.save, port: 5000, notes: "scanned" });
      renderSaved();
    });
  });
  el.querySelectorAll("[data-forget]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("/api/devices/forget", { method: "POST", body: { ip: b.dataset.forget } });
      refreshDevices();
    }));
}

async function verifyDevice(proto, ip) {
  const out = $("dev-verify");
  out.textContent = `${proto} ${ip}: probing…`;
  try {
    let r;
    if (proto === "meshcore")
      r = await api("/api/protocols/meshcore/read", { method: "POST", body: { host: ip, port: 5000 } });
    else
      r = await api("/api/protocols/meshtastic/read", { method: "POST", body: { host: ip } });
    out.textContent = `${ip}: ` + JSON.stringify(r, null, 2).slice(0, 1600);
  } catch (e) { out.textContent = `${ip}: ${e.message}`; }
}

function renderSaved() {
  $("dev-saved").innerHTML = savedDevices.map((d, i) =>
    `<div class="unit"><span class="dot ok"></span>
     <strong>${esc(d.name || "?")}</strong><code>${esc(d.host)}:${esc(d.port || 5000)}</code>
     <span class="muted small">${esc(d.notes || "")}</span>
     <button class="ghost" data-ver="${i}">verify</button>
     <button class="ghost danger" data-rem="${i}">remove</button></div>`).join("")
    || "<div class='muted small'>no saved devices</div>";
  document.querySelectorAll("[data-rem]").forEach((b) =>
    b.addEventListener("click", () => { savedDevices.splice(+b.dataset.rem, 1); renderSaved(); }));
  document.querySelectorAll("[data-ver]").forEach((b) =>
    b.addEventListener("click", () => {
      const d = savedDevices[+b.dataset.ver];
      verifyDevice((+d.port === 4403) ? "meshtastic" : "meshcore", d.host);
    }));
}

$("dev-add").addEventListener("click", () => {
  const host = $("dev-host").value.trim();
  if (!host) return;
  let [h, p] = host.split(":");
  savedDevices.push({ name: $("dev-name").value.trim() || h, host: h, port: +(p || 5000), notes: "" });
  $("dev-host").value = ""; $("dev-name").value = "";
  renderSaved();
});

$("dev-save-all").addEventListener("click", async () => {
  const r = await api("/api/devices/save", { method: "POST", body: { devices: savedDevices } });
  showResult("dev-msg", `saved ${r.devices.length} devices`, !!r.ok);
});
/* ---------------- logs ---------------- */
async function refreshAudit() {
  try {
    const r = await api("/api/audit?limit=80", { method: "GET" });
    $("audit-out").textContent = (r.entries || []).map((e) =>
      `${e.iso}  ${e.user}  ${e.action}  ${e.detail || ""}`).join("\n") || "no audit entries";
  } catch (e) { /* ignore */ }
}
$("audit-refresh").addEventListener("click", refreshAudit);

initSession();
