// Redy Dashboard — proposals + chat notification metrics.
// Data is generated client-side as a stand-in for a real API.
// Replace `loadData()` with a fetch() call when wiring to the backend.

const NAMES = [
  "Avery Chen", "Marcus Lee", "Priya Patel", "Sofia Romero", "Daniel Kim",
  "Jordan Blake", "Hana Suzuki", "Liam O'Brien", "Naomi Reyes", "Oscar Webb",
];
const ADDRESSES = [
  "412 Maple Ave", "88 Harbor Pl", "1701 Elm St", "23 Cedar Ct",
  "905 Birch Way", "57 Lakeview Dr", "1290 Sunset Blvd", "318 Oak Ln",
];

function rand(seed) {
  // mulberry32 — deterministic per range so refresh is stable
  let t = seed >>> 0;
  return () => {
    t += 0x6D2B79F5;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

function loadData(days) {
  const r = rand(days * 1009 + 7);
  const series = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    series.push({
      date: d,
      proposals: Math.floor(r() * 14) + (days === 1 ? 1 : 3),
      seller:    Math.floor(r() * 22) + 4,
      agent:     Math.floor(r() * 18) + 3,
    });
  }

  const feed = [];
  const total = days === 1 ? 8 : 18;
  for (let i = 0; i < total; i++) {
    const kinds = ["proposal", "seller", "agent"];
    const kind = kinds[Math.floor(r() * kinds.length)];
    const minsAgo = Math.floor(r() * (days * 24 * 60));
    const when = new Date(Date.now() - minsAgo * 60 * 1000);
    feed.push({
      kind,
      who: NAMES[Math.floor(r() * NAMES.length)],
      addr: ADDRESSES[Math.floor(r() * ADDRESSES.length)],
      when,
      unread: r() < 0.45,
    });
  }
  feed.sort((a, b) => b.when - a.when);
  return { series, feed };
}

function sum(series, key) { return series.reduce((s, p) => s + p[key], 0); }

function fmtDelta(curr, prev) {
  if (prev === 0) return { text: "—", cls: "" };
  const pct = Math.round(((curr - prev) / prev) * 100);
  const cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
  const sign = pct > 0 ? "+" : "";
  return { text: `${sign}${pct}% vs prior period`, cls };
}

function renderKpis(series, days) {
  const prev = loadData(days * 2).series.slice(0, days);
  const proposals = sum(series, "proposals");
  const seller    = sum(series, "seller");
  const agent     = sum(series, "agent");

  const set = (id, val, delta) => {
    document.getElementById(id).textContent = val.toLocaleString();
    if (delta) {
      const el = document.getElementById(id + "-delta");
      el.textContent = delta.text;
      el.className = "kpi-delta " + delta.cls;
    }
  };

  set("kpi-proposals", proposals, fmtDelta(proposals, sum(prev, "proposals")));
  set("kpi-seller",    seller,    fmtDelta(seller,    sum(prev, "seller")));
  set("kpi-agent",     agent,     fmtDelta(agent,     sum(prev, "agent")));
  document.getElementById("kpi-unread").textContent =
    Math.round((proposals + seller + agent) * 0.18).toLocaleString();
}

function renderChart(series) {
  const svg = document.getElementById("chart");
  const W = 760, H = 260, PAD_L = 36, PAD_R = 12, PAD_T = 16, PAD_B = 28;
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;
  const max = Math.max(1, ...series.flatMap(p => [p.proposals, p.seller, p.agent])) * 1.15;
  const x = i => PAD_L + (series.length === 1 ? innerW / 2 : (i * innerW) / (series.length - 1));
  const y = v => PAD_T + innerH - (v / max) * innerH;

  const line = (key, color) => {
    const d = series.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(p[key]).toFixed(1)}`).join(" ");
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round" />`;
  };
  const dots = (key, color) => series.map((p, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(p[key]).toFixed(1)}" r="2.5" fill="${color}" />`
  ).join("");

  // gridlines + y labels
  const gridSteps = 4;
  let grid = "";
  for (let i = 0; i <= gridSteps; i++) {
    const gy = PAD_T + (innerH * i) / gridSteps;
    const v = Math.round(max - (max * i) / gridSteps);
    grid += `<line x1="${PAD_L}" x2="${W - PAD_R}" y1="${gy}" y2="${gy}" stroke="#262b35" stroke-dasharray="3 3" />`;
    grid += `<text x="${PAD_L - 6}" y="${gy + 3}" fill="#8b93a3" font-size="10" text-anchor="end">${v}</text>`;
  }

  // x labels (skip some on long ranges)
  const stepX = Math.ceil(series.length / 7);
  let xlabels = "";
  series.forEach((p, i) => {
    if (i % stepX !== 0 && i !== series.length - 1) return;
    const label = p.date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    xlabels += `<text x="${x(i)}" y="${H - 8}" fill="#8b93a3" font-size="10" text-anchor="middle">${label}</text>`;
  });

  svg.innerHTML = grid + xlabels +
    line("proposals", "#8b5cf6") + dots("proposals", "#8b5cf6") +
    line("seller",    "#14b8a6") + dots("seller",    "#14b8a6") +
    line("agent",     "#f59e0b") + dots("agent",     "#f59e0b");
}

function timeAgo(d) {
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

let currentFeed = [];
let currentTab = "all";

function renderFeed() {
  const ul = document.getElementById("feed");
  const items = currentFeed.filter(i => currentTab === "all" || i.kind === currentTab);
  if (items.length === 0) {
    ul.innerHTML = `<li style="color:#8b93a3;justify-content:center">No notifications.</li>`;
    return;
  }
  ul.innerHTML = items.map(i => {
    const label = i.kind === "proposal" ? "P" : i.kind === "seller" ? "S" : "A";
    const text = i.kind === "proposal"
      ? `submitted a proposal for <b>${i.addr}</b>`
      : i.kind === "seller"
        ? `(seller) sent a chat about <b>${i.addr}</b>`
        : `(agent) replied in chat about <b>${i.addr}</b>`;
    return `<li>
      <span class="badge ${i.kind}">${label}</span>
      <div>
        <div class="who">${i.who}${i.unread ? '<span class="unread" title="unread"></span>' : ''}</div>
        <div class="what">${text}</div>
      </div>
      <span class="when">${timeAgo(i.when)}</span>
    </li>`;
  }).join("");
}

function refresh() {
  const days = parseInt(document.getElementById("range").value, 10);
  const { series, feed } = loadData(days);
  currentFeed = feed;
  renderKpis(series, days);
  renderChart(series);
  renderFeed();
}

document.getElementById("range").addEventListener("change", refresh);
document.getElementById("refresh").addEventListener("click", refresh);
document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    currentTab = t.dataset.tab;
    renderFeed();
  });
});

refresh();
