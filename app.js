// Redy Dashboard — proposals + chat notification metrics.
// Data is sourced from BigQuery (`redy_prod_analytics`) via fetch_data.py
// which writes data/dashboard.json. Rendering slices that file by date range.

const DATA_URL = "data/dashboard.json";

let DATA = null;        // { series: [...], feed: [...] }
let currentFeed = [];
let currentTab = "all";

async function fetchData() {
  const res = await fetch(DATA_URL, { cache: "no-cache" });
  if (!res.ok) throw new Error(`Failed to load ${DATA_URL}: ${res.status}`);
  const raw = await res.json();
  // Parse dates once.
  return {
    ...raw,
    series: raw.series.map(p => ({ ...p, date: new Date(p.date + "T00:00:00") })),
    feed: raw.feed.map(i => ({ ...i, when: new Date(i.when * 1000) })),
  };
}

function sliceByDays(series, days) {
  // series is sorted ascending; return the trailing `days` entries.
  return series.slice(Math.max(0, series.length - days));
}

function priorPeriod(series, days) {
  const end = Math.max(0, series.length - days);
  return series.slice(Math.max(0, end - days), end);
}

function feedWithinDays(feed, days) {
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  return feed.filter(i => i.when.getTime() >= cutoff);
}

function sum(series, key) { return series.reduce((s, p) => s + (p[key] || 0), 0); }

function fmtDelta(curr, prev) {
  if (prev === 0) return { text: curr > 0 ? "new activity" : "—", cls: curr > 0 ? "up" : "" };
  const pct = Math.round(((curr - prev) / prev) * 100);
  const cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
  const sign = pct > 0 ? "+" : "";
  return { text: `${sign}${pct}% vs prior period`, cls };
}

function renderKpis(series, prev) {
  const proposals = sum(series, "proposals");
  const seller    = sum(series, "seller");
  const agent     = sum(series, "agent");
  const unread    = sum(series, "unread_bids") + sum(series, "unread_msgs");

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
  document.getElementById("kpi-unread").textContent = unread.toLocaleString();
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

  const gridSteps = 4;
  let grid = "";
  for (let i = 0; i <= gridSteps; i++) {
    const gy = PAD_T + (innerH * i) / gridSteps;
    const v = Math.round(max - (max * i) / gridSteps);
    grid += `<line x1="${PAD_L}" x2="${W - PAD_R}" y1="${gy}" y2="${gy}" stroke="#262b35" stroke-dasharray="3 3" />`;
    grid += `<text x="${PAD_L - 6}" y="${gy + 3}" fill="#8b93a3" font-size="10" text-anchor="end">${v}</text>`;
  }

  const stepX = Math.max(1, Math.ceil(series.length / 7));
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
  if (s < 60) return Math.max(0, s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

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
    const who = i.who || "—";
    return `<li>
      <span class="badge ${i.kind}">${label}</span>
      <div>
        <div class="who">${who}${i.unread ? '<span class="unread" title="unread"></span>' : ''}</div>
        <div class="what">${text}</div>
      </div>
      <span class="when">${timeAgo(i.when)}</span>
    </li>`;
  }).join("");
}

function renderError(msg) {
  document.getElementById("feed").innerHTML =
    `<li style="color:#ef4444;justify-content:center">${msg}</li>`;
  ["kpi-proposals","kpi-seller","kpi-agent","kpi-unread"].forEach(id => {
    document.getElementById(id).textContent = "—";
  });
  document.getElementById("chart").innerHTML = "";
}

function refresh() {
  if (!DATA) return;
  const days = parseInt(document.getElementById("range").value, 10);
  const series = sliceByDays(DATA.series, days);
  const prev   = priorPeriod(DATA.series, days);
  currentFeed = feedWithinDays(DATA.feed, days);
  renderKpis(series, prev);
  renderChart(series);
  renderFeed();
}

document.getElementById("range").addEventListener("change", refresh);
document.getElementById("refresh").addEventListener("click", async () => {
  try { DATA = await fetchData(); refresh(); }
  catch (e) { renderError(e.message); }
});
document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    currentTab = t.dataset.tab;
    renderFeed();
  });
});

(async () => {
  try {
    DATA = await fetchData();
    refresh();
  } catch (e) {
    renderError(`Could not load dashboard data (${e.message}). ` +
      `Run \`python3 fetch_data.py\` to regenerate data/dashboard.json.`);
  }
})();
