const DATA_URL = "data/dashboard.json";
let DATA = null, PAGE = 0;
const PER_PAGE = 10;
const OWNER_COLORS = ["#43AF49","#2563EB","#D97706","#7C3AED","#DB2777","#0D9488","#6366F1","#EA580C"];
const ICON = {phone:"\u{1F4DE}",message:"\u{1F4AC}","message-2":"\u{1F4AC}","file-text":"\u{1F4C4}",home:"\u{1F3E0}","alert-triangle":"\u26A0\uFE0F"};

async function loadData() {
  const r = await fetch(DATA_URL, {cache:"no-cache"});
  if (!r.ok) throw new Error(`${r.status}`);
  const d = await r.json();
  d.feed = (d.feed||[]).map(f => ({...f, when: new Date(f.when * 1000)}));
  return d;
}

function populateFilters() {
  const os = document.getElementById("f-owner");
  (DATA.owners||[]).forEach(o => { const opt = document.createElement("option"); opt.value = o.id; opt.textContent = o.name; os.appendChild(opt); });

  // Multi-select status dropdown
  const dd = document.getElementById("f-status-dd");
  const display = document.getElementById("f-status-display");
  const wrap = document.getElementById("f-status-wrap");
  const statusCounts = {};
  (DATA.listings||[]).forEach(l => { statusCounts[l.status] = (statusCounts[l.status]||0)+1; });
  (DATA.statuses||[]).forEach(s => {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = s;
    cb.addEventListener("change", () => { updateStatusDisplay(); PAGE=0; render(); });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(` ${s.replace(/_/g," ")} (${statusCounts[s]||0})`));
    dd.appendChild(label);
  });
  display.addEventListener("click", (e) => { e.stopPropagation(); dd.style.display = dd.style.display==="none"?"block":"none"; });
  document.addEventListener("click", () => { dd.style.display = "none"; });
  dd.addEventListener("click", (e) => { e.stopPropagation(); });

  document.getElementById("sync-time").textContent = "Last sync: " + (DATA.generated_at||"").replace("T"," ").replace("Z"," UTC");
  const today = new Date().toISOString().split("T")[0];
  const monthAgo = new Date(Date.now()-30*86400000).toISOString().split("T")[0];
  document.getElementById("f-from").value = monthAgo;
  document.getElementById("f-to").value = today;
}

function updateStatusDisplay() {
  const checked = Array.from(document.querySelectorAll('#f-status-dd input[type="checkbox"]:checked'));
  const display = document.getElementById("f-status-display");
  if (checked.length === 0) { display.textContent = "All statuses"; }
  else if (checked.length === 1) { display.textContent = checked[0].value.replace(/_/g," "); }
  else { display.textContent = checked.length + " statuses selected"; }
}

function getFilters() {
  const statusEls = document.querySelectorAll('#f-status-dd input[type="checkbox"]:checked');
  const statuses = Array.from(statusEls).map(e => e.value);
  return {
    owner: document.getElementById("f-owner").value,
    statuses: statuses,
    urgency: document.getElementById("f-urgency").value,
    type: document.getElementById("f-type").value,
    from: document.getElementById("f-from").value,
    to: document.getElementById("f-to").value,
  };
}

function applyFilters(listings) {
  const f = getFilters();
  return listings.filter(l => {
    if (f.owner && l.owner_id !== f.owner) return false;
    if (f.statuses.length > 0 && !f.statuses.includes(l.status)) return false;
    if (f.urgency === "none") { if (l.tasks.length > 0) return false; }
    else if (f.urgency) { const a = f.urgency.split(","); if (!l.tasks.some(t => a.includes(t.urgency))) return false; }
    if (f.type && !l.tasks.some(t => t.type === f.type)) return false;
    if (f.from && l.created_date && l.created_date < f.from) return false;
    if (f.to && l.created_date && l.created_date > f.to) return false;
    return true;
  });
}

function renderKpis(filtered) {
  const allTasks = filtered.flatMap(l => l.tasks);
  const sel = document.getElementById("f-owner");
  const label = sel.value ? sel.options[sel.selectedIndex].text : "all team";
  document.getElementById("k-listings").textContent = filtered.length;
  document.getElementById("k-listings-sub").textContent = `assigned to ${label}`;
  document.getElementById("k-tasks").textContent = allTasks.length;
  document.getElementById("k-tasks-sub").textContent = `across ${filtered.filter(l=>l.tasks.length>0).length} listings`;
  const uc = allTasks.filter(t=>t.urgency==="urgent").length;
  const el = document.getElementById("k-urgent"); el.textContent = uc; el.className = "kpi-value" + (uc>0?" red":"");
  document.getElementById("k-warn").textContent = allTasks.filter(t=>t.urgency==="warning").length;
  const todayProposals = DATA.feed ? DATA.feed.filter(f=>f.kind==="proposal").length : 0;
  document.getElementById("k-proposals").textContent = todayProposals;
  document.getElementById("k-proposals-sub").textContent = "received today";
}

function fmtPrice(p) { if (!p) return ""; if (p>=1e6) return `$${(p/1e6).toFixed(2)}M`; if (p>=1e3) return `$${Math.round(p/1e3)}K`; return `$${p}`; }

function taskHtml(t) {
  const cls = t.urgency==="urgent"?"t-urgent":t.urgency==="warning"?"t-warning":"t-info";
  const icon = ICON[t.icon]||"\u{1F4CB}";
  return `<div class="task-item ${cls}"><span class="task-icon">${icon}</span><span style="flex:1">${t.text}</span><span class="task-time">${t.age_label}</span></div>`;
}

function renderTable(filtered) {
  const start = PAGE * PER_PAGE;
  const page = filtered.slice(start, start + PER_PAGE);
  const tbody = document.getElementById("tbody");
  if (!page.length) { tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No listings match your filters.</td></tr>`; document.getElementById("pagination").innerHTML=""; return; }

  tbody.innerHTML = page.map((l,i) => {
    const tasks = l.tasks.length > 0
      ? `<div class="tasks-cell">${l.tasks.map(taskHtml).join("")}</div>`
      : `<div class="no-tasks">\u2705 All clear — no open tasks</div>`;
    const oi = (DATA.owners||[]).findIndex(o=>o.id===l.owner_id);
    const oc = OWNER_COLORS[oi>=0?oi%OWNER_COLORS.length:0];
    return `<tr>
      <td><div class="addr">${l.address||"Unknown"}</div><div class="addr-sub">${l.city}, ${l.state} ${l.zip} · ${fmtPrice(l.price)}</div></td>
      <td><span class="status-pill sp-${l.status}">${l.status.replace(/_/g," ")}</span></td>
      <td><div class="owner-cell"><span class="owner-av" style="background:${oc}20;color:${oc}">${l.owner_initials}</span><span style="font-weight:500">${l.owner_name.split(" ")[0]}</span></div></td>
      <td style="font-size:12px">${l.seller_name||""}</td>
      <td>${tasks}</td>
      <td><a class="view-link" href="#">View →</a></td>
    </tr>`;
  }).join("");

  const total = filtered.length, pages = Math.ceil(total/PER_PAGE);
  document.getElementById("pagination").innerHTML = `
    <span>Showing ${start+1}–${Math.min(start+PER_PAGE,total)} of ${total} listings</span>
    <span><button ${PAGE===0?"disabled":""} id="pg-prev">← Prev</button> <button ${PAGE>=pages-1?"disabled":""} id="pg-next">Next →</button></span>`;
  document.getElementById("pg-prev")?.addEventListener("click",()=>{PAGE--;render()});
  document.getElementById("pg-next")?.addEventListener("click",()=>{PAGE++;render()});
}

function timeAgo(d) {
  const s = Math.floor((Date.now()-d.getTime())/1000);
  if (s<60) return "just now"; if (s<3600) return Math.floor(s/60)+"m ago";
  if (s<86400) return Math.floor(s/3600)+"h ago"; return Math.floor(s/86400)+"d ago";
}

function renderFeed(filtered) {
  const ul = document.getElementById("feed");
  const lids = new Set(filtered.map(l=>l.listing_id));
  const ownerFilter = document.getElementById("f-owner").value;
  let items = DATA.feed||[];
  if (ownerFilter && lids.size>0) items = items.filter(f=>!f.listing_id||lids.has(f.listing_id));
  items = items.slice(0,20);
  if (!items.length) { ul.innerHTML=`<li style="color:var(--muted);justify-content:center;display:flex">No recent activity.</li>`; return; }
  ul.innerHTML = items.map(i => {
    const icon = i.kind==="proposal"?"P":i.kind==="seller"?"S":"A";
    const text = i.kind==="proposal"?`submitted a proposal for`:i.kind==="seller"?`(seller) sent a chat about`:`(agent) sent a message about`;
    return `<li><span class="badge ${i.kind}">${icon}</span><div><div class="who">${i.who||"—"}${i.unread?'<span class="unread"></span>':""}</div><div class="what">${text} <b>${i.addr||""}</b></div></div><span class="when">${timeAgo(i.when)}</span></li>`;
  }).join("");
}

function render() { if (!DATA) return; const f = applyFilters(DATA.listings||[]); renderKpis(f); renderTable(f); renderFeed(f); }
function renderError(msg) { document.getElementById("tbody").innerHTML=`<tr><td colspan="6" class="empty-state" style="color:#DC2626">${msg}</td></tr>`; }

["f-owner","f-urgency","f-type"].forEach(id=>document.getElementById(id).addEventListener("change",()=>{PAGE=0;render()}));
["f-from","f-to"].forEach(id=>document.getElementById(id).addEventListener("change",()=>{PAGE=0;render()}));
document.getElementById("clear-filters").addEventListener("click",()=>{
  ["f-owner","f-urgency","f-type"].forEach(id=>{document.getElementById(id).value=""});
  document.querySelectorAll('#f-status-dd input[type="checkbox"]').forEach(cb=>{cb.checked=false});
  updateStatusDisplay();
  const today=new Date().toISOString().split("T")[0], mo=new Date(Date.now()-30*86400000).toISOString().split("T")[0];
  document.getElementById("f-from").value=mo; document.getElementById("f-to").value=today; PAGE=0; render();
});
document.getElementById("refresh").addEventListener("click",async()=>{try{DATA=await loadData();render()}catch(e){renderError(e.message)}});

(async()=>{
  try { DATA = await loadData(); populateFilters(); render(); }
  catch(e) { renderError(`Could not load data (${e.message}). Run <code>python3 fetch_data.py</code> to generate data.`); }
})();
