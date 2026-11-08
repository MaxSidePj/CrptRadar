"use strict";
let DATA = null;
const LIVE = {}; // address(lowercase) -> latest DexScreener pair, fetched by this phone
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function usd(v) {
  if (v == null || isNaN(v)) return "?";
  if (v >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return "$" + Math.round(v / 1e3) + "k";
  return "$" + Math.round(v);
}
function age(h) {
  if (h == null) return "?";
  if (h < 1) return Math.round(h * 60) + "m";
  if (h < 48) return h.toFixed(1) + "h";
  return Math.round(h / 24) + "d";
}
function ago(ts) {
  const m = (Date.now() / 1000 - ts) / 60;
  if (m < 1) return "just now";
  if (m < 60) return Math.round(m) + " min ago";
  if (m < 48 * 60) return Math.round(m / 60) + " h ago";
  return Math.round(m / 1440) + " d ago";
}
const scoreClass = (s) => (s >= 70 ? "s-good" : s >= 50 ? "s-mid" : "s-bad");
const pct = (v) => (v == null ? "?" : (v > 0 ? "+" : "") + Math.round(v) + "%");

function linkButtons(links) {
  const names = { chart: "📈 Chart", bubblemap: "🫧 Bubblemap", rugcheck: "🛡 RugCheck", goplus: "🛡 GoPlus",
    x_search: "𝕏 Live posts", x: "𝕏 Account", telegram: "✈️ Telegram", website: "🌐 Website" };
  return Object.entries(links || {}).map(([k, u]) =>
    `<a href="${esc(u)}" target="_blank" rel="noopener">${names[k] || esc(k)}</a>`).join("");
}

function safetyTags(e) {
  const m = e.safety || {};
  const tags = [];
  if (!e.verified) tags.push(`<span class="tag warn">safety unchecked</span>`);
  (e.rejects || []).forEach((r) => tags.push(`<span class="tag bad">✕ ${esc(r)}</span>`));
  (e.warnings || []).forEach((w) => tags.push(`<span class="tag warn">⚠ ${esc(w)}</span>`));
  if (e.verified && !(e.rejects || []).length) {
    tags.push(`<span class="tag good">✓ ${esc(m.source || "checked")}</span>`);
    if (m.top10_pct != null) tags.push(`<span class="tag">top10 ${m.top10_pct.toFixed(0)}%</span>`);
    const dev = Math.max(m.creator_pct || 0, m.owner_pct || 0);
    tags.push(`<span class="tag">dev ${dev.toFixed(1)}%</span>`);
    if (m.bundle_pct != null) tags.push(`<span class="tag">bundles ${m.bundle_pct.toFixed(0)}%</span>`);
    if (m.lp_locked_pct != null && m.lp_locked_pct >= 0) tags.push(`<span class="tag">LP safe ${m.lp_locked_pct.toFixed(0)}%</span>`);
    if (m.launchpad) tags.push(`<span class="tag">${esc(m.launchpad)}</span>`);
  }
  if (e.trending) tags.push(`<span class="tag good">trending</span>`);
  if (e.fomo && typeof e.fomo === "object" && e.fomo.buyers_6h) tags.push(`<span class="tag good">🔥 FOMO ×${e.fomo.buyers_6h}</span>`);
  if (e.cto) tags.push(`<span class="tag good">community takeover</span>`);
  if (e.cg_trending) tags.push(`<span class="tag good">CoinGecko trending</span>`);
  if (e.boosts) tags.push(`<span class="tag">boosted</span>`);
  if (e.biz) tags.push(`<span class="tag">/biz/ ×${e.biz}</span>`);
  return tags.join("");
}

function bars(parts) {
  const max = { staying: 25, traction: 20, fomo: 15, holders: 13, social: 12, momentum: 10, upside: 5 };
  const label = { staying: "Staying", traction: "Traction", fomo: "FOMO app", holders: "Holders", social: "Hype",
    momentum: "Momentum", upside: "Upside" };
  return Object.keys(max).map((k) => {
    const v = Math.max(0, (parts || {})[k] || 0);
    return `<div class="bar"><b>${label[k]}</b><i><em style="width:${Math.min(100, (v / max[k]) * 100)}%"></em></i><span>${v.toFixed(0)}/${max[k]}</span></div>`;
  }).join("");
}

function fomoLine(f) {
  if (!f || typeof f !== "object") return "";
  const who = (f.top_buyers || []).slice(0, 4).map(esc).join(", ");
  return `<p class="fomo">🔥 <b>FOMO app:</b> ${f.buyers_6h} traders bought in 6h${who ? " (" + who + ")" : ""}` +
    `${f.sellers_6h ? `, ${f.sellers_6h} sold` : ""}${f.rank ? ` · #${f.rank} trending` : ""}` +
    `${f.theses_24h ? ` · ${f.theses_24h} thesis posts` : ""}</p>`;
}

function gateList(gates, title = "Staying-power checks") {
  if (!gates || !gates.length) return "";
  return `<div class="gates"><b>${title}</b>${gates.map((g) =>
    `<div class="${g.ok ? "up" : "down"}">${g.ok ? "✓" : "✕"} ${esc(g.text)}</div>`).join("")}</div>`;
}

function tokenCard(e, extra = "") {
  const m = e.safety || {};
  const img = e.image ? `<img src="${esc(e.image)}" loading="lazy" alt="">` : `<div class="ph"></div>`;
  return `<div class="card" data-key="${esc(e.key)}">
    <div class="row">${img}
      <div class="mid"><div class="sym">$${esc(e.symbol)} <span class="muted">${esc(e.chain)}</span></div>
        <div class="sub">MC ${usd(e.mcap)} · Liq ${usd(e.liq)} · ${age(e.age_h)} old · 1h ${pct((e.pc || {}).h1)}</div></div>
      <div class="score ${scoreClass(e.score)}">${Math.round(e.score)}</div></div>
    <div class="detail">${extra}
      <div class="grid">
        <div><span>Volume 1h / 24h</span>${usd((e.vol || {}).h1)} / ${usd((e.vol || {}).h24)}</div>
        <div><span>Buys / sells 1h</span>${e.buys_h1 ?? "?"} / ${e.sells_h1 ?? "?"}</div>
        <div><span>Price 5m / 6h</span>${pct((e.pc || {}).m5)} / ${pct((e.pc || {}).h6)}</div>
        <div><span>Holders</span>${m.holders ? m.holders.toLocaleString() : "–"}</div>
        <div><span>X followers</span>${e.x ? e.x.followers.toLocaleString() : "–"}</div>
        <div><span>Checked</span>${e.checked ? ago(e.checked) : "?"}</div>
      </div>
      ${fomoLine(e.fomo)}
      ${e.buzz != null ? `<div class="bar"><b>Buzz</b><i><em style="width:${e.buzz}%;background:var(--mid)"></em></i><span>${e.buzz}/100</span></div>` : ""}
      <div class="bars">${bars(e.parts)}</div>
      ${gateList(e.gates)}
      ${e.fast_gates && !(e.gates || []).every((g) => g.ok) ? gateList(e.fast_gates, "FOMO fast-lane checks (young coins)") : ""}
      ${(e.why || []).length ? `<p class="muted">Why: ${esc(e.why.join(", "))}</p>` : ""}
      <div class="tags">${safetyTags(e)}</div>
      <div class="addr">${esc(e.address)}</div>
      <div class="links">${linkButtons(e.links)}</div>
    </div></div>`;
}

function renderAlerts() {
  const list = DATA.alerts || [];
  const s = DATA.stats?.last_cycle || {};
  let html = `<div class="stat"><div><b>${DATA.alerts_today ?? 0}/${DATA.config.max_alerts_per_day}</b><span>alerts today</span></div>
    <div><b>${s.seeds ?? "–"}</b><span>tokens last scan</span></div><div><b>${(s.chains || []).length}</b><span>chains</span></div></div>`;
  if (!list.length) html += `<div class="empty">No alerts yet. GemRadar only alerts when a coin passes every scam check and scores ${DATA.config.alert_min_score}+.<br>See the <b>Live</b> tab for what it's watching.</div>`;
  html += list.map((a) => {
    const board = (DATA.board || []).find((b) => b.key === a.key);
    const e = { ...a, ...(board || {}), score: a.score, links: a.links, kind: a.kind, ts: a.ts,
      safety: board?.safety || a.safety, parts: board?.parts || a.parts, verified: true, rejects: [],
      warnings: a.warnings, checked: board?.checked, gates: a.gates || board?.gates,
      fomo: (board?.fomo && typeof board.fomo === "object") ? board.fomo : (a.fomo && typeof a.fomo === "object" ? a.fomo : null),
      buzz: board?.buzz ?? a.buzz, fast_gates: board?.fast_gates || a.fast_gates };
    applyLive(e, LIVE[(a.address || "").toLowerCase()], a.live);
    const extra = `<p><b>${{ gem: "💎 Gem", survivor: "🏆 Survivor", fomo: "🔥 FOMO hype" }[a.kind] || "🚀 Runner"}</b> · alerted ${ago(a.ts)} at MC ${usd(a.mcap)}
      · now ${perfText(e.mcap_now, a.mcap)}</p>`;
    return tokenCard(e, extra);
  }).join("");
  const open = document.querySelector("#alerts .card.open")?.dataset.key;
  $("#alerts").innerHTML = html;
  if (open) document.querySelector(`#alerts .card[data-key="${CSS.escape(open)}"]`)?.classList.add("open");
}

function perfText(now, then) {
  if (!now) return `<span class="perf">…</span>`;
  const ch = then > 0 ? (now / then - 1) * 100 : null;
  const cls = ch == null ? "" : ch >= 0 ? "up" : "down";
  return `<span class="perf ${cls}">${usd(now)}${ch == null ? "" : ` (${ch >= 0 ? "+" : ""}${ch.toFixed(0)}%)`}</span>`;
}

// Current market numbers: this phone's own fetch first, else the scanner's last refresh.
function applyLive(e, p, live) {
  if (p) {
    const tx = p.txns?.h1 || {};
    e.mcap = e.mcap_now = p.marketCap || p.fdv;
    e.liq = p.liquidity?.usd;
    e.vol = { m5: p.volume?.m5, h1: p.volume?.h1, h6: p.volume?.h6, h24: p.volume?.h24 };
    e.pc = { m5: p.priceChange?.m5, h1: p.priceChange?.h1, h6: p.priceChange?.h6, h24: p.priceChange?.h24 };
    e.buys_h1 = tx.buys; e.sells_h1 = tx.sells;
    if (p.pairCreatedAt) e.age_h = (Date.now() - p.pairCreatedAt) / 3.6e6;
    if (!e.image && p.info?.imageUrl) e.image = p.info.imageUrl;
    e.checked = Date.now() / 1000;
  } else if (live) {
    Object.assign(e, { mcap: live.mcap, liq: live.liq, vol: live.vol, pc: live.pc, buys_h1: live.buys_h1,
      sells_h1: live.sells_h1, checked: live.ts });
    e.mcap_now = live.mcap;
    if (live.age_h != null) e.age_h = live.age_h;
  } else if (e.age_h != null && e.ts) {
    e.age_h = e.age_h + (Date.now() / 1000 - e.ts) / 3600; // age keeps counting since the alert
  }
}

async function fetchLive(list) {
  const byChain = {};
  list.slice(0, 60).forEach((a) => (byChain[a.chain] = byChain[a.chain] || []).push(a.address));
  for (const [chain, addrs] of Object.entries(byChain)) {
    for (let i = 0; i < addrs.length; i += 30) {
      try {
        const r = await fetch(`https://api.dexscreener.com/tokens/v1/${chain}/${addrs.slice(i, i + 30).join(",")}`);
        (await r.json()).forEach((p) => {
          const a = (p.baseToken?.address || "").toLowerCase();
          if (!LIVE[a] || (p.liquidity?.usd || 0) > (LIVE[a].liquidity?.usd || 0)) LIVE[a] = p;
        });
      } catch (_) { /* offline: keep the scanner's numbers */ }
    }
  }
  renderAlerts();
}

function renderBoard() {
  const chain = $("#chainFilter").value;
  const young = $("#youngOnly").checked;
  const list = (DATA.board || []).filter((e) => (!chain || e.chain === chain) && (!young || e.age_h <= 24));
  $("#boardList").innerHTML = list.length ? list.map((e) => tokenCard(e)).join("")
    : `<div class="empty">Nothing matches right now.</div>`;
}

function renderBlocked() {
  const list = DATA.rejected || [];
  $("#blocked").innerHTML = `<p class="muted">Tokens that looked active but failed a scam check in the last hours. These never trigger alerts.</p>` +
    (list.length ? list.map((e) => tokenCard(e)).join("") : `<div class="empty">Nothing blocked recently.</div>`);
}

function render() {
  $("#updated").textContent = "Updated " + ago(DATA.updated);
  $("#minScore").textContent = DATA.config.alert_min_score;
  $("#maxDay").textContent = DATA.config.max_alerts_per_day;
  const chains = [...new Set((DATA.board || []).map((e) => e.chain))].sort();
  const sel = $("#chainFilter"), cur = sel.value;
  sel.innerHTML = `<option value="">All chains</option>` + chains.map((c) => `<option ${c === cur ? "selected" : ""}>${esc(c)}</option>`).join("");
  renderAlerts(); renderBoard(); renderBlocked();
  fetchLive(DATA.alerts || []);
}

async function load() {
  try {
    const r = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
    DATA = await r.json();
    render();
  } catch (e) {
    $("#updated").textContent = "Could not load data yet";
  }
}

document.addEventListener("click", (ev) => {
  const tab = ev.target.closest("nav button");
  if (tab) {
    document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b === tab));
    document.querySelectorAll("main > section").forEach((s) => (s.hidden = s.id !== tab.dataset.tab));
    document.querySelectorAll(".card.open").forEach((c) => c.classList.remove("open"));
    window.scrollTo(0, 0);
    return;
  }
  const row = ev.target.closest(".row");
  if (row) {
    const card = row.parentElement;
    const open = !card.classList.contains("open");
    document.querySelectorAll(".card.open").forEach((c) => c.classList.remove("open"));
    if (open) card.classList.add("open");
  }
});
$("#chainFilter").addEventListener("change", renderBoard);
$("#youngOnly").addEventListener("change", renderBoard);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
load();
setInterval(load, 120000);
