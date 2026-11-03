const HOMES = { station: "/discover", contributor: "/contributor", partner: "/partner" };

async function me() {
  try {
    const r = await fetch("/api/me");
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  }
}

async function guard(role) {
  const u = await me();
  if (!u) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    return null;
  }
  if (u.role !== role) {
    location.href = HOMES[u.role] + "?denied=1";
    return null;
  }
  if (new URLSearchParams(location.search).get("denied") === "1") showDenied(u);
  return u;
}

function showDenied(u) {
  const c = document.querySelector(".content");
  if (!c) return;
  const d = document.createElement("div");
  d.className = "notice";
  d.textContent = "That area is restricted. You're signed in as " + u.name + " (" + u.role + ").";
  c.prepend(d);
}

function topUser(u) {
  const a = document.getElementById("avatar");
  if (a) a.textContent = u.name.split(/[\s@]+/).map(function (w) { return w[0]; }).join("").slice(0, 2).toUpperCase();
  const s = document.getElementById("stationName");
  if (s) s.textContent = u.org || u.name;
}

async function logout() {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function money(n) {
  return "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

const EST_MS = 5 * 3600 * 1000;

function estParts(s) {
  const t = Date.parse(s || "");
  if (!t) return null;
  return new Date(t - EST_MS).toISOString();
}

function estDate(s) {
  const iso = estParts(s);
  return iso ? iso.slice(0, 10) : "—";
}

function estStamp(s) {
  const iso = estParts(s);
  return iso ? iso.slice(0, 10) + " " + iso.slice(11, 16) : "—";
}

function estNowStamp(d) {
  return (d ? new Date(d.getTime() - EST_MS) : new Date(Date.now() - EST_MS)).toUTCString().slice(5, 25).toUpperCase() + " EST";
}

function tickClock(id) {
  const el = document.getElementById(id || "clock");
  if (!el) return;
  const draw = function () { el.textContent = estNowStamp(); };
  draw();
  setInterval(draw, 1000);
}

function tzLine(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const draw = function () {
    const d = new Date(Date.now() - EST_MS);
    let h = d.getUTCHours();
    const ap = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    el.textContent = h + ":" + String(d.getUTCMinutes()).padStart(2, "0") + " " + ap + " EST";
  };
  draw();
  setInterval(draw, 30000);
}

function todayEST() {
  return new Date(Date.now() - EST_MS).toISOString().slice(0, 10);
}

function timeAgo(s) {
  const t = Date.parse(s || "");
  if (!t) return "";
  const mins = Math.max(0, Math.floor((Date.now() - t) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + "h ago";
  const days = Math.floor(hours / 24);
  if (days < 7) return days + "d ago";
  return new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

const BELL_KINDS = { application: "New application", delivery: "Delivery ready", note: "New note", pitch: "New pitch", brief: "New brief", assigned: "Brief assigned", changes: "Changes requested", approved: "Delivery approved", booking: "New booking" };

async function notifBell() {
  const bar = document.querySelector(".topbar");
  if (!bar || document.getElementById("bellBtn")) return;
  const btn = document.createElement("button");
  btn.className = "bell";
  btn.id = "bellBtn";
  btn.title = "Notifications";
  btn.innerHTML = "<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9'/><path d='M13.73 21a2 2 0 0 1-3.46 0'/></svg><span class='count' id='bellCount' hidden></span>";
  const avatar = document.getElementById("avatar");
  bar.insertBefore(btn, avatar);
  const panel = document.createElement("div");
  panel.className = "bell-panel";
  panel.id = "bellPanel";
  panel.hidden = true;
  document.body.appendChild(panel);
  let items = [];
  async function refresh() {
    let data = null;
    try {
      const r = await fetch("/api/notifications");
      data = r.ok ? await r.json() : null;
    } catch (e) { data = null; }
    if (!data) return;
    items = data.items || [];
    const badge = document.getElementById("bellCount");
    badge.hidden = !data.unread;
    badge.textContent = data.unread || "";
    if (!panel.hidden) render();
  }
  function render() {
    panel.innerHTML = "<div class='bell-head'><span>Notifications</span><button id='bellRead'>Mark all read</button></div>" +
      (items.length ? "" : "<p class='bell-empty'>Nothing here yet — new activity will appear here.</p>");
    items.forEach(n => {
      const a = document.createElement("a");
      a.className = "bell-item" + (n.unread ? " unread" : "");
      a.href = n.link || "/requests";
      a.innerHTML = "<span class='kind'>" + esc(BELL_KINDS[n.kind] || "Update") + "</span><b>" + esc(n.title) + "</b>" +
        (n.detail ? "<span class='sub'>" + esc(n.detail.slice(0, 140)) + "</span>" : "") +
        "<span class='when'>" + esc(timeAgo(n.at)) + "</span>";
      panel.appendChild(a);
    });
    document.getElementById("bellRead").onclick = async (e) => {
      e.stopPropagation();
      await fetch("/api/notifications/seen", { method: "POST" });
      items.forEach(n => n.unread = false);
      document.getElementById("bellCount").hidden = true;
      render();
    };
  }
  btn.onclick = (e) => {
    e.stopPropagation();
    panel.hidden = !panel.hidden;
    if (!panel.hidden) render();
  };
  panel.onclick = (e) => e.stopPropagation();
  document.addEventListener("click", () => { panel.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") panel.hidden = true; });
  refresh();
  setInterval(refresh, 60000);
}

const ICONS = {
  compass: "<circle cx='12' cy='12' r='10'/><polygon points='16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76'/>",
  bookmark: "<path d='M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z'/>",
  calendar: "<rect x='3' y='4' width='18' height='18' rx='2'/><line x1='16' y1='2' x2='16' y2='6'/><line x1='8' y1='2' x2='8' y2='6'/><line x1='3' y1='10' x2='21' y2='10'/>",
  filetext: "<path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'/><polyline points='14 2 14 8 20 8'/><line x1='16' y1='13' x2='8' y2='13'/><line x1='16' y1='17' x2='8' y2='17'/>",
  inbox: "<polyline points='22 12 16 12 14 15 10 15 8 12 2 12'/><path d='M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'/>",
  pluscircle: "<circle cx='12' cy='12' r='10'/><line x1='12' y1='8' x2='12' y2='16'/><line x1='8' y1='12' x2='16' y2='12'/>",
  plus: "<line x1='12' y1='5' x2='12' y2='19'/><line x1='5' y1='12' x2='19' y2='12'/>",
  user: "<path d='M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2'/><circle cx='12' cy='7' r='4'/>",
  users: "<path d='M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2'/><circle cx='9' cy='7' r='4'/><path d='M23 21v-2a4 4 0 0 0-3-3.87'/><path d='M16 3.13a4 4 0 0 1 0 7.75'/>",
  grid: "<rect x='3' y='3' width='7' height='7'/><rect x='14' y='3' width='7' height='7'/><rect x='14' y='14' width='7' height='7'/><rect x='3' y='14' width='7' height='7'/>",
  clipboard: "<path d='M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2'/><rect x='8' y='2' width='8' height='4' rx='1'/>",
  upload: "<path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/><polyline points='17 8 12 3 7 8'/><line x1='12' y1='3' x2='12' y2='15'/>",
  download: "<path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/><polyline points='7 10 12 15 17 10'/><line x1='12' y1='15' x2='12' y2='3'/>",
  image: "<rect x='3' y='3' width='18' height='18' rx='2'/><circle cx='8.5' cy='8.5' r='1.5'/><polyline points='21 15 16 10 5 21'/>",
  dollar: "<line x1='12' y1='1' x2='12' y2='23'/><path d='M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6'/>",
  sliders: "<line x1='4' y1='21' x2='4' y2='14'/><line x1='4' y1='10' x2='4' y2='3'/><line x1='12' y1='21' x2='12' y2='12'/><line x1='12' y1='8' x2='12' y2='3'/><line x1='20' y1='21' x2='20' y2='16'/><line x1='20' y1='12' x2='20' y2='3'/><line x1='1' y1='14' x2='7' y2='14'/><line x1='9' y1='8' x2='15' y2='8'/><line x1='17' y1='16' x2='23' y2='16'/>",
  logout: "<path d='M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4'/><polyline points='16 17 21 12 16 7'/><line x1='21' y1='12' x2='9' y2='12'/>",
  video: "<polygon points='23 7 16 12 23 16 23 7'/><rect x='1' y='5' width='15' height='14' rx='2'/>",
  activity: "<polyline points='22 12 18 12 15 21 9 3 6 12 2 12'/>",
  check: "<polyline points='20 6 9 17 4 12'/>",
  checkcircle: "<path d='M22 11.08V12a10 10 0 1 1-5.93-9.14'/><polyline points='22 4 12 14.01 9 11.01'/>",
  x: "<line x1='18' y1='6' x2='6' y2='18'/><line x1='6' y1='6' x2='18' y2='18'/>",
  alert: "<path d='M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'/><line x1='12' y1='9' x2='12' y2='13'/><line x1='12' y1='17' x2='12.01' y2='17'/>",
  info: "<circle cx='12' cy='12' r='10'/><line x1='12' y1='16' x2='12' y2='12'/><line x1='12' y1='8' x2='12.01' y2='8'/>",
  search: "<circle cx='11' cy='11' r='8'/><line x1='21' y1='21' x2='16.65' y2='16.65'/>",
  play: "<polygon points='5 3 19 12 5 21 5 3'/>",
  send: "<line x1='22' y1='2' x2='11' y2='13'/><polygon points='22 2 15 22 11 13 2 9 22 2'/>",
  pin: "<path d='M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z'/><circle cx='12' cy='10' r='3'/>",
  file: "<path d='M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z'/><polyline points='13 2 13 9 20 9'/>",
  save: "<path d='M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z'/><polyline points='17 21 17 13 7 13 7 21'/><polyline points='7 3 7 8 15 8'/>",
  paperclip: "<path d='M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48'/>",
  zap: "<polygon points='13 2 3 14 12 14 11 22 21 10 12 10 13 2'/>",
  share: "<circle cx='18' cy='5' r='3'/><circle cx='6' cy='12' r='3'/><circle cx='18' cy='19' r='3'/><line x1='8.59' y1='13.51' x2='15.42' y2='17.49'/><line x1='15.41' y1='6.51' x2='8.59' y2='10.49'/>",
  external: "<path d='M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6'/><polyline points='15 3 21 3 21 9'/><line x1='10' y1='14' x2='21' y2='3'/>",
  arrowup: "<line x1='12' y1='19' x2='12' y2='5'/><polyline points='5 12 12 5 19 12'/>",
  package: "<path d='M16.5 9.4L7.55 4.24'/><path d='M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z'/><polyline points='3.27 6.96 12 12.01 20.73 6.96'/><line x1='12' y1='22.08' x2='12' y2='12'/>",
  circle: "<circle cx='12' cy='12' r='10'/>",
  radio: "<circle cx='12' cy='12' r='2'/><path d='M16.24 7.76a6 6 0 0 1 0 8.49'/><path d='M7.76 16.24a6 6 0 0 1 0-8.49'/><path d='M19.07 4.93a10 10 0 0 1 0 14.14'/><path d='M4.93 19.07a10 10 0 0 1 0-14.14'/>",
  camera: "<path d='M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z'/><circle cx='12' cy='13' r='4'/>"
};

function icon(name, size) {
  const s = size || 14;
  return "<svg class='ic' width='" + s + "' height='" + s + "' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>" + (ICONS[name] || "") + "</svg>";
}

function ccAid(src) {
  const m = /^\/assets\/([^\/ ?#]+)/.exec(src || "");
  return m ? m[1] : null;
}

function cc608Wrap(text) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  words.forEach(w => {
    while (w.length > 32) {
      if (line) { lines.push(line); line = ""; }
      lines.push(w.slice(0, 32));
      w = w.slice(32);
    }
    if ((line + " " + w).trim().length > 32) {
      lines.push(line);
      line = w;
    } else {
      line = (line + " " + w).trim();
    }
  });
  if (line) lines.push(line);
  return lines.slice(0, 2);
}

async function attachRollup(video, aid) {
  if (!aid || !video || video.dataset.rollup) return;
  video.dataset.rollup = "1";
  let stage = null;
  try {
    if (getComputedStyle(video).position === "absolute" && video.parentNode) {
      stage = video.parentNode;
      if (getComputedStyle(stage).position === "static") stage.style.position = "relative";
    } else if (video.parentNode) {
      stage = document.createElement("div");
      stage.className = "cc608-wrap";
      video.parentNode.insertBefore(stage, video);
      stage.appendChild(video);
    } else {
      return;
    }
  } catch (e) { return; }
  const cap = document.createElement("div");
  cap.className = "cc608";
  cap.setAttribute("aria-live", "polite");
  cap.style.display = "none";
  stage.appendChild(cap);
  let cues = null;
  const draw = () => {
    if (!cues) return;
    const t = video.currentTime || 0;
    let rows = [];
    cues.filter(c => c.start <= t && t <= c.end + 2.5).slice(-3).forEach(c => {
      rows = rows.concat(cc608Wrap(c.text));
    });
    rows = rows.slice(-3);
    cap.innerHTML = rows.map(r => "<span>" + esc(r) + "</span>").join("");
    cap.style.display = rows.length ? "" : "none";
    if (video.offsetWidth) {
      cap.style.left = video.offsetLeft + "px";
      cap.style.width = video.offsetWidth + "px";
      cap.style.right = "auto";
    }
  };
  const ready = async () => {
    try {
      const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/captions/cues");
      const body = r.ok ? await r.json().catch(() => ({})) : {};
      cues = (body.cues || []).filter(c => c && typeof c.start === "number")
        .sort((a, b) => a.start - b.start || a.end - b.end);
    } catch (e) { cues = []; }
    if (!cues.length) { cap.style.display = "none"; return; }
    video.addEventListener("timeupdate", draw);
    video.addEventListener("seeked", draw);
    draw();
  };
  let status = null;
  try {
    const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/captions",
      { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ auto: true }) });
    status = r.ok ? (await r.json().catch(() => ({}))).status : null;
  } catch (e) { return; }
  if (status === "ready") { ready(); return; }
  if (status !== "pending") return;
  cap.innerHTML = "<span>Generating captions…</span>";
  cap.style.display = "";
  const poll = async () => {
    let body = {};
    try {
      const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/captions");
      body = r.ok ? await r.json() : {};
    } catch (e) { return; }
    if (body.status === "ready") {
      ready();
    } else if (body.status === "pending") {
      setTimeout(poll, 4000);
    } else {
      cap.style.display = "none";
    }
  };
  setTimeout(poll, 4000);
}

function aiPillFor(ai) {
  ai = ai || {};
  const label = ai.label || (ai.verdict === "ai" ? "POSSIBLY AI-MADE" : null);
  if (!label) return "";
  const cls = (ai.tone || "red") === "amber" ? "flag-amber" : "p-live";
  return "<span class='pill " + cls + "' title='" + Math.round((ai.score || 0) * 100) + "% of " + (ai.judged || 0) + " checked frames look synthetic'>" + icon("info", 11) + " " + esc(label) + "</span>";
}

function modPillFor(st, n) {
  if (st === "clean") return "<span class='pill p-lic'>" + icon("checkcircle", 11) + " CHECKED · CLEAN</span>";
  if (st === "flagged") {
    const label = n ? (n + (n === 1 ? " SCENE BLURRED" : " SCENES BLURRED")) : "FLAGGED · BLURRED";
    return "<span class='pill p-live'>" + icon("alert", 11) + " " + esc(label) + "</span>";
  }
  if (st === "pending") return "<span class='pill flag-gray'>" + icon("info", 11) + " SCANNING…</span>";
  return "";
}

function modStamp(s) {
  s = Math.max(0, Math.round(s || 0));
  return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
}

async function modCcPill(aid) {
  try {
    const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/captions");
    const body = r.ok ? await r.json() : {};
    if ((body.status || "none") === "ready") {
      return "<span class='pill p-lic'>" + icon("checkcircle", 11) + " CC READY</span>";
    }
  } catch (e) {}
  return "";
}

async function enhanceMod(box, forStation) {
  const aid = box.dataset.aid;
  if (!aid || box.dataset.done) return;
  box.dataset.done = "1";
  let body = {};
  try {
    const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/moderation");
    body = r.ok ? await r.json() : {};
  } catch (e) { return; }
  const st = body.status || "none";
  if (st === "pending") {
    box.innerHTML = "<p class='fine'>Scanning for sensitive content…</p>";
    box.dataset.done = "";
    setTimeout(() => enhanceMod(box, forStation), 4000);
    return;
  }
  if (st === "flagged") {
    const flags = body.flags || [];
    const card = box.closest(".brief, .room") || document;
    const video = card.querySelector("video");
    const tagCls = c => (c === "nudity" || c === "sexual-activity") ? "flag-red" : (c === "unverified" ? "flag-gray" : "flag-amber");
    const chips = video ? flags.map(f => "<button class='flag-tag " + tagCls(f.category) + " seek' data-t='" + f.start + "' title='Jump to " + modStamp(f.start) + "'>" + icon("alert", 11) + " " + modStamp(f.start) + "-" + modStamp(f.end) + " · " + esc(f.category || "flagged") + "</button>").join("") : "";
    const stamps = video ? "" : " Flagged " + flags.map(f => modStamp(f.start) + "-" + modStamp(f.end)).join(", ") + ".";
    const note = forStation
      ? "The preview on this page shows the blurred copy."
      : "The station reviews a blurred copy of these scenes.";
    const aiPill = aiPillFor(body.ai);
    const ccPill = await modCcPill(aid);
    box.innerHTML = "<div class='warn'>" + icon("alert", 14) + " " + flags.length + " scene" + (flags.length === 1 ? "" : "s") + " auto-blurred for broadcast. " + note + stamps +
      ((chips || aiPill || ccPill) ? "<div class='flag-tags'>" + chips + aiPill + ccPill + "</div>" : "") + "</div>";
    if (forStation && video && video.src.indexOf("review=1") < 0) {
      video.src = "/assets/" + encodeURIComponent(aid) + "?review=1";
      video.load();
    }
    box.querySelectorAll(".seek").forEach(b => b.onclick = () => {
      const v = (box.closest(".brief, .room") || document).querySelector("video");
      if (!v) return;
      v.currentTime = parseFloat(b.dataset.t) || 0;
      v.play().catch(() => {});
      v.scrollIntoView({ block: "nearest" });
    });
    return;
  }
  if (st === "clean") {
    const aiPill = aiPillFor(body.ai);
    const ccPill = await modCcPill(aid);
    box.innerHTML = "<p><span class='pill p-lic'>" + icon("checkcircle", 11) + " CHECKED · CLEAN</span>" + (aiPill ? " " + aiPill : "") + (ccPill ? " " + ccPill : "") + "</p>";
    return;
  }
  if (!box.dataset.video || !forStation) return;
  const scanCc = await modCcPill(aid);
  box.innerHTML = (st === "error" ? "<p class='fine'>Last scan failed" + (body.error ? ": " + esc(body.error) : ".") + " </p>" : "") +
    "<button class='btn btn-ghost modscan' style='width:auto;padding:4px 10px;font-size:12px'>Scan for sensitive content</button>" + (scanCc ? " " + scanCc : "");
  box.querySelector(".modscan").onclick = async () => {
    const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/moderation", { method: "POST" });
    if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.error || "Couldn't start the scan."); return; }
    box.dataset.done = "";
    enhanceMod(box, forStation);
  };
}

async function enhanceCc(box) {
  const aid = box.dataset.aid;
  if (!aid || box.dataset.done) return;
  box.dataset.done = "1";
  let body = {};
  try {
    const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/captions");
    body = r.ok ? await r.json() : {};
  } catch (e) { return; }
  const st = body.status || "none";
  if (st === "ready") {
    const card = box.closest(".brief, .room");
    if (card && card.querySelector(".modbox")) { box.innerHTML = ""; return; }
    box.innerHTML = "<p><span class='pill p-lic'>" + icon("checkcircle", 11) + " CC READY</span></p>";
    return;
  }
  if (st === "pending") {
    box.innerHTML = "<p class='fine'>Captioning…</p>";
    box.dataset.done = "";
    setTimeout(() => enhanceCc(box), 4000);
    return;
  }
  if (!box.dataset.video) return;
  box.innerHTML = (st === "error" ? "<p class='fine'>Last captioning failed" + (body.error ? ": " + esc(body.error) : ".") + " </p>" : "") +
    "<button class='btn btn-ghost ccscan' style='width:auto;padding:4px 10px;font-size:12px'>Generate captions</button>";
  box.querySelector(".ccscan").onclick = async () => {
    const r = await fetch("/api/assets/" + encodeURIComponent(aid) + "/captions", { method: "POST" });
    if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.error || "Couldn't start captioning."); return; }
    box.dataset.done = "";
    enhanceCc(box);
  };
}

function hydrateIcons() {
  document.querySelectorAll("i[data-ic]").forEach(el => {
    const tmp = document.createElement("span");
    tmp.innerHTML = icon(el.getAttribute("data-ic"), +(el.getAttribute("data-size") || 14));
    const svg = tmp.firstChild;
    if (el.className) svg.setAttribute("class", "ic " + el.className);
    el.replaceWith(svg);
  });
}

const NAV_ICONS = {
  "/discover": ICONS.compass,
  "/discover?saved=1": ICONS.bookmark,
  "/bookings": ICONS.calendar,
  "/expenses": ICONS.filetext,
  "/requests": ICONS.inbox,
  "/request": ICONS.pluscircle,
  "/station": ICONS.user,
  "/contributor": ICONS.grid,
  "/assignments": ICONS.clipboard,
  "/pitches": ICONS.send,
  "/publish": ICONS.upload,
  "/listings": ICONS.package,
  "/media": ICONS.image,
  "/sales": ICONS.dollar,
  "/profile": ICONS.sliders,
  "#": ICONS.logout,
  "/partner": ICONS.grid,
  "#cameras": ICONS.video,
  "#usage": ICONS.activity,
  "#payouts": ICONS.dollar,
  "#settings": ICONS.sliders
};

function navIcons() {
  document.querySelectorAll(".nav a").forEach(a => {
    if (a.querySelector("svg")) return;
    const href = a.getAttribute("href") || "";
    const icon = NAV_ICONS[href] || NAV_ICONS[href.split("?")[0]] || null;
    if (!icon) return;
    const label = a.innerHTML.replace(/^[^A-Za-z0-9<]+/, "");
    a.innerHTML = "<svg width='15' height='15' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>" + icon + "</svg>" + label;
  });
}

function hydrateAll() { navIcons(); hydrateIcons(); }

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", hydrateAll);
else hydrateAll();
