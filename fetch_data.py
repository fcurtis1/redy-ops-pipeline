#!/usr/bin/env python3
"""Redy Ops Dashboard — data pipeline.

HubSpot = primary (listings, owners, stages, create dates, tasks, properties)
BigQuery = enrichment (proposals, chat messages, outbound tracking)

Setup:
    pip install google-cloud-bigquery requests
    gcloud auth application-default login
    export HUBSPOT_API_KEY=pat-na1-xxxxx
    python3 fetch_data.py
"""
import json, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None
    print("⚠ google-cloud-bigquery not installed — tasks will be empty.", file=sys.stderr)

# ─── Config ───────────────────────────────────────────────────────────
HUBSPOT_KEY = os.environ.get("HUBSPOT_API_KEY", "")
if not HUBSPOT_KEY:
    sys.exit("Set HUBSPOT_API_KEY env var")

BQ_PROJECT = "redy-core-platform-prod"
BQ_DATASET = "redy_prod_analytics"
OUT = Path(__file__).parent / "data" / "dashboard.json"
T = f"`{BQ_PROJECT}.{BQ_DATASET}"
HS = "https://api.hubapi.com"
HDR = {"Authorization": f"Bearer {HUBSPOT_KEY}", "Content-Type": "application/json"}
HS_PORTAL = "8349742"
PDT = timezone(timedelta(hours=-7))

# ─── Full pipeline mapping (HubSpot stage ID → BigQuery status) ──────
STAGE_MAP = {
    "23944048": "saved",               # Profile Incomplete
    "23508262": "pending_approval",     # Pending Verification
    "23508265": "active",              # Proposal Collection Period
    "25849360": "bid_review",          # Proposal Review
    "29494160": "agent_offered",       # Agent Selected
    "listing_signed_id": "listing_signed",  # Listing Signed (update ID)
    "agent_fees_id": "agent_fees_collected", # Agent Fees Collected (update ID)
    "29976183": "completed",           # Redy Reward Paid
    "29976185": "cancelled",           # Cancelled
    "29974172": "rejected",            # Rejected
}

# Reverse: BigQuery status → HubSpot label (for display)
STATUS_LABELS = {
    "saved": "Profile Incomplete",
    "pending_approval": "Pending Verification",
    "active": "Proposal Collection",
    "bid_review": "Proposal Review",
    "agent_offered": "Agent Selected",
    "listing_signed": "Listing Signed",
    "agent_fees_collected": "Agent Fees Collected",
    "completed": "Redy Reward Paid",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
    "aged_lead": "Aged Lead",
}

# Active pipeline statuses (shown on dashboard)
ACTIVE_STATUSES = {"saved", "pending_approval", "active", "bid_review", "agent_offered"}


# ─── HubSpot helpers ──────────────────────────────────────────────────
def hs_get(path, params=None):
    r = requests.get(f"{HS}{path}", headers=HDR, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def hs_post(path, body):
    r = requests.post(f"{HS}{path}", headers=HDR, json=body, timeout=30)
    r.raise_for_status()
    return r.json()

def hs_get_owners():
    owners = {}
    after = None
    while True:
        params = {"limit": 100}
        if after: params["after"] = after
        data = hs_get("/crm/v3/owners", params)
        for o in data.get("results", []):
            oid = str(o["id"])
            fn, ln = o.get("firstName", ""), o.get("lastName", "")
            owners[oid] = {"id": oid, "name": f"{fn} {ln}".strip(), "initials": (fn[:1]+ln[:1]).upper()}
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break
    return owners

def hs_get_stages():
    """Discover all pipeline stages dynamically."""
    stages = {}
    data = hs_get("/crm/v3/pipelines/deals")
    for pipeline in data.get("results", []):
        for stage in pipeline.get("stages", []):
            stages[stage["id"]] = stage.get("label", stage["id"])
    return stages

def hs_search_deals(date_from_ms=None):
    """Pull all deals from HubSpot with pagination."""
    all_deals, after = [], None
    props = [
        "dealname", "dealstage", "hubspot_owner_id", "createdate", "amount",
        "allow_agent_reach_out", "selected_agent_to_meet", "assigned_agent",
    ]
    body_base = {"properties": props, "limit": 100,
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}]}
    if date_from_ms:
        body_base["filterGroups"] = [{"filters": [
            {"propertyName": "createdate", "operator": "GTE", "value": str(date_from_ms)}]}]
    while True:
        body = {**body_base}
        if after: body["after"] = after
        data = hs_post("/crm/v3/objects/deals/search", body)
        results = data.get("results", [])
        all_deals.extend(results)
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after or not results: break
        time.sleep(0.1)
    return all_deals

def hs_get_open_tasks():
    """Get all NOT_STARTED tasks and their associated deals."""
    all_tasks, after = [], None
    props = ["hs_task_subject", "hs_task_status", "hs_task_priority",
             "hubspot_owner_id", "hs_task_type", "hs_timestamp"]
    body_base = {"properties": props, "limit": 100,
        "filterGroups": [{"filters": [
            {"propertyName": "hs_task_status", "operator": "EQ", "value": "NOT_STARTED"}]}],
        "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}]}
    while True:
        body = {**body_base}
        if after: body["after"] = after
        data = hs_post("/crm/v3/objects/tasks/search", body)
        results = data.get("results", [])
        all_tasks.extend(results)
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after or not results: break
        time.sleep(0.1)
    return all_tasks

def parse_deal(deal, owners, stage_map_dynamic):
    props = deal.get("properties", {})
    dealname = props.get("dealname", "")
    address, city, state, zipcode = "", "", "", ""
    if "| " in dealname:
        parts = dealname.split("| ", 1)[1].split(", ")
        if len(parts) >= 4:
            address, city, state = parts[0], parts[1], parts[2]
            zipcode = parts[3].split(" ")[0] if len(parts) > 3 else ""
        elif len(parts) >= 2:
            address, city = parts[0], parts[1]

    stage_id = props.get("dealstage", "")
    status = stage_map_dynamic.get(stage_id, stage_id)

    owner_id = str(props.get("hubspot_owner_id", "") or "")
    owner = owners.get(owner_id, {"name": "Unassigned", "initials": "—"})

    # Convert create date to PDT
    created = props.get("createdate", "")
    created_date = ""
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            created_date = dt.astimezone(PDT).strftime("%Y-%m-%d")
        except Exception:
            created_date = created[:10]

    price = 0
    try: price = int(float(props.get("amount", 0) or 0))
    except: pass

    return {
        "hubspot_deal_id": str(deal["id"]),
        "listing_id": str(deal["id"]),
        "address": address, "city": city, "state": state, "zip": zipcode,
        "price": price, "status": status, "created_date": created_date,
        "owner_id": owner_id, "owner_name": owner.get("name", "Unassigned"),
        "owner_initials": owner.get("initials", "—"), "seller_name": "",
        "allow_agent_reach_out": props.get("allow_agent_reach_out", ""),
        "selected_agent_to_meet": props.get("selected_agent_to_meet", ""),
        "assigned_agent": props.get("assigned_agent", ""),
        "tasks": [], "last_activity_label": "",
    }


# ─── BigQuery enrichment ──────────────────────────────────────────────
def get_bq_data():
    if not bigquery:
        return {}, {}, set(), {}
    bq = bigquery.Client(project=BQ_PROJECT)

    print("  BQ: bids...")
    bids = {}
    for r in bq.query(f"""
        SELECT CAST(l.hubspot_deal_id AS STRING) AS did,
          COUNTIF(NOT COALESCE(b.viewed,FALSE) AND b.status='placed') AS unviewed,
          COUNT(*) AS total_bids,
          STRING_AGG(DISTINCT CONCAT(u.first_name,' ',LEFT(u.last_name,1),'.') 
            ORDER BY CONCAT(u.first_name,' ',LEFT(u.last_name,1),'.') LIMIT 4) AS agent_names,
          MIN(b.created_at) AS oldest_bid
        FROM {T}.stg_bids` b
        LEFT JOIN {T}.stg_users` u ON b.agent_id=u.user_id
        LEFT JOIN {T}.stg_listings` l ON b.listing_id=l.listing_id
        WHERE LOWER(b.status) NOT IN ('withdrawn','cancel','cancelled')
          AND l.hubspot_deal_id IS NOT NULL
        GROUP BY l.hubspot_deal_id
    """).result():
        if r.did:
            age_h = 0
            if r.oldest_bid:
                age_h = (datetime.now(timezone.utc) - r.oldest_bid.replace(tzinfo=timezone.utc)).total_seconds() / 3600
            bids[r.did] = {"unviewed": r.unviewed or 0, "total_bids": r.total_bids or 0,
                           "agent_names": r.agent_names or "", "age_hours": round(age_h, 1)}

    print("  BQ: messages...")
    msgs = {}
    for r in bq.query(f"""
        SELECT CAST(l.hubspot_deal_id AS STRING) AS did,
          COUNTIF(u.role='seller' AND m.read_at IS NULL) AS unread_seller,
          COUNTIF(u.role='agent' AND m.read_at IS NULL) AS unread_agent
        FROM {T}.stg_chat_messages` m
        LEFT JOIN {T}.stg_users` u ON m.sender_id=u.user_id
        LEFT JOIN {T}.stg_bids` b ON m.bid_id=b.bid_id
        LEFT JOIN {T}.stg_listings` l ON b.listing_id=l.listing_id
        WHERE m.read_at IS NULL AND l.hubspot_deal_id IS NOT NULL
        GROUP BY l.hubspot_deal_id
    """).result():
        if r.did:
            msgs[r.did] = {"unread_seller": r.unread_seller or 0, "unread_agent": r.unread_agent or 0}

    print("  BQ: outbound...")
    outbound = set()
    for r in bq.query(f"""
        SELECT DISTINCT CAST(l.hubspot_deal_id AS STRING) AS did
        FROM {T}.stg_chat_messages` m
        JOIN {T}.stg_bids` b ON m.bid_id=b.bid_id
        JOIN {T}.stg_listings` l ON b.listing_id=l.listing_id
        WHERE m.receiver_id=l.owner_id AND l.hubspot_deal_id IS NOT NULL
    """).result():
        outbound.add(r.did)

    print("  BQ: sellers...")
    sellers = {}
    for r in bq.query(f"""
        SELECT CAST(l.hubspot_deal_id AS STRING) AS did,
          CONCAT(COALESCE(u.first_name,''),' ',LEFT(COALESCE(u.last_name,''),1),'.') AS name
        FROM {T}.stg_listings` l
        LEFT JOIN {T}.stg_users` u ON l.owner_id=u.user_id
        WHERE l.hubspot_deal_id IS NOT NULL
    """).result():
        if r.did and r.name and r.name.strip():
            sellers[r.did] = r.name.strip()

    return bids, msgs, outbound, sellers


# ─── Task rules (Talia's ops rules) ──────────────────────────────────
def build_tasks(rec, bids, msgs, outbound, hs_tasks_by_deal):
    did = rec["hubspot_deal_id"]
    seller = rec["seller_name"] or "Seller"
    status = rec["status"]
    tasks = []
    b = bids.get(did, {})
    m = msgs.get(did, {})
    uv = b.get("unviewed", 0)
    total = b.get("total_bids", 0)
    us = m.get("unread_seller", 0)
    ua = m.get("unread_agent", 0)
    has_ob = did in outbound
    age_h = b.get("age_hours", 0)
    allow_reach = (rec.get("allow_agent_reach_out", "") or "").lower() == "yes"
    agent_selected = bool(rec.get("selected_agent_to_meet", ""))

    # ── Rule 5: Profile Incomplete → email template ──
    if status == "saved":
        urgency = "urgent" if age_h > 120 else "warning"  # 5 days
        tasks.append({"type": "profile_incomplete", "urgency": urgency, "icon": "📧",
            "text": f"Email profile incomplete template to {seller} — no phone number",
            "age_label": "action needed", "age_hours": age_h})

    # ── Existing: Unread seller messages → reply ──
    if us > 0:
        tasks.append({"type": "reply_seller", "urgency": "urgent", "icon": "📞",
            "text": f"Reply to {seller} (seller) — {us} unread message{'s'*(us!=1)}",
            "age_label": "urgent", "age_hours": 25})

    # ── Existing: Unread agent messages → reply ──
    if ua > 0:
        tasks.append({"type": "reply_agent", "urgency": "urgent", "icon": "💬",
            "text": f"Reply to {ua} unread agent message{'s'*(ua!=1)}",
            "age_label": "urgent", "age_hours": 25})

    # ── Rule 1: Seller approved outreach, agent hasn't connected ──
    if status == "bid_review" and allow_reach and ua == 0 and not has_ob:
        urgency = "urgent" if age_h > 120 else "warning"
        tasks.append({"type": "follow_up_agents", "urgency": urgency, "icon": "📞",
            "text": f"Seller approved agent outreach — follow up with agents, confirm they're reaching out to {seller}",
            "age_label": f"{int(age_h/24)}d" if age_h > 24 else "today", "age_hours": age_h})

    # ── Rule 2: Proposals waiting, seller hasn't approved outreach ──
    if status == "bid_review" and uv > 0 and not allow_reach:
        urgency = "urgent" if age_h > 168 else "warning" if age_h > 72 else "info"
        tasks.append({"type": "call_seller_approve", "urgency": urgency, "icon": "📞",
            "text": f"Call {seller} — {uv} proposal{'s'*(uv!=1)} waiting, ask why no agent selected to meet",
            "age_label": f"{int(age_h/24)}d" if age_h > 24 else "today", "age_hours": age_h})

    # ── Rule 3: Low/no proposals after 3 days ──
    if status in ("active", "bid_review") and total <= 1 and age_h > 72:
        tasks.append({"type": "low_proposals", "urgency": "warning", "icon": "⚠️",
            "text": f"Only {total} proposal{'s'*(total!=1)} after {int(age_h/24)}d — tag Adrian, extend bidding window 48h",
            "age_label": f"{int(age_h/24)}d", "age_hours": age_h})

    # ── Rule 4: Agent selected for walkthrough, no confirmation ──
    if agent_selected and status in ("bid_review", "agent_offered"):
        tasks.append({"type": "confirm_walkthrough", "urgency": "warning", "icon": "🏠",
            "text": f"Seller selected agent for walkthrough — confirm meeting is scheduled",
            "age_label": "action needed", "age_hours": 48})

    # ── Existing: New proposals → notify seller ──
    if uv > 0 and not has_ob and status != "saved":
        agents = b.get("agent_names", "")
        tasks.append({"type": "new_proposals", "urgency": "info", "icon": "📄",
            "text": f"{uv} new proposal{'s'*(uv!=1)} from agents — notify seller {seller}" +
                    (f" ({agents})" if agents else ""),
            "age_label": "today", "age_hours": 0})

    # ── Rule 6: HubSpot pending tasks ──
    for ht in hs_tasks_by_deal.get(did, []):
        subj = ht.get("subject", "Task")
        priority = ht.get("priority", "NONE")
        urgency = "urgent" if priority == "HIGH" else "warning" if priority == "MEDIUM" else "info"
        tasks.append({"type": "hs_task", "urgency": urgency, "icon": "✅",
            "text": f"HubSpot task: {subj}",
            "age_label": "open", "age_hours": 0})

    # Sort: urgent first, then by age
    order = {"urgent": 0, "warning": 1, "info": 2}
    tasks.sort(key=lambda t: (order.get(t["urgency"], 9), -t.get("age_hours", 0)))
    return tasks


# ─── Main ─────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)

    # 1. HubSpot owners
    print("1. HubSpot owners...")
    owners = hs_get_owners()
    print(f"   {len(owners)} owners")

    # 2. Pipeline stages (dynamic discovery)
    print("2. Pipeline stages...")
    hs_stages = hs_get_stages()
    # Build dynamic stage map: stage_id → normalized status
    stage_map = {}
    label_lower_map = {
        "profile incomplete": "saved",
        "pending verification": "pending_approval",
        "proposal collection period": "active", "proposal collection": "active",
        "proposal review": "bid_review",
        "agent selected": "agent_offered",
        "listing signed": "listing_signed",
        "agent fees collected": "agent_fees_collected",
        "redy reward paid": "completed",
        "cancelled": "cancelled", "canceled": "cancelled",
        "rejected": "rejected",
        "aged lead": "aged_lead",
    }
    for sid, label in hs_stages.items():
        normalized = label_lower_map.get(label.lower().strip())
        if normalized:
            stage_map[sid] = normalized
        else:
            stage_map[sid] = label.lower().replace(" ", "_")
        print(f"   {sid}: {label} → {stage_map[sid]}")

    # 3. Fetch deals (last 90 days)
    print("3. Fetching deals...")
    cutoff_ms = int((now.timestamp() - 90 * 86400) * 1000)
    deals = hs_search_deals(date_from_ms=cutoff_ms)
    print(f"   {len(deals)} deals")

    # 4. Parse into records
    print("4. Parsing...")
    records = [parse_deal(d, owners, stage_map) for d in deals]
    # Keep only active pipeline statuses
    records = [r for r in records if r["status"] in ACTIVE_STATUSES]
    print(f"   {len(records)} active pipeline listings")

    # 5. BigQuery enrichment
    print("5. BigQuery enrichment...")
    bids, msgs, outbound, sellers = get_bq_data()
    print(f"   {len(bids)} with bids, {len(msgs)} with messages, {len(sellers)} seller names")
    for r in records:
        did = r["hubspot_deal_id"]
        if did in sellers:
            r["seller_name"] = sellers[did]

    # 6. HubSpot tasks
    print("6. HubSpot tasks...")
    hs_tasks = hs_get_open_tasks()
    print(f"   {len(hs_tasks)} open tasks")
    # TODO: Associate tasks with deals via HubSpot associations API
    # For now, group by owner_id (tasks don't easily map to deals via search)
    hs_tasks_by_deal = {}  # Will be populated when association data is available

    # 7. Build tasks
    print("7. Building tasks...")
    for r in records:
        r["tasks"] = build_tasks(r, bids, msgs, outbound, hs_tasks_by_deal)
        # Clean internal fields
        for key in ("allow_agent_reach_out", "selected_agent_to_meet", "assigned_agent"):
            r.pop(key, None)

    # Sort: urgent tasks first
    order = {"urgent": 0, "warning": 1, "info": 2}
    records.sort(key=lambda r: (
        min((order.get(t["urgency"], 9) for t in r["tasks"]), default=3),
        -max((t.get("age_hours", 0) for t in r["tasks"]), default=0)))

    # 8. Output
    print("8. Writing output...")
    own = {}
    for r in records:
        if r["owner_id"] and r["owner_id"] not in own:
            own[r["owner_id"]] = {"id": r["owner_id"], "name": r["owner_name"], "initials": r["owner_initials"]}

    all_t = [t for r in records for t in r["tasks"]]
    payload = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "HubSpot (primary) + BigQuery (tasks)",
        "portal_id": HS_PORTAL,
        "owners": sorted(own.values(), key=lambda o: o["name"]),
        "statuses": sorted({r["status"] for r in records}),
        "kpis": {
            "total_listings": len(records),
            "total_tasks": len(all_t),
            "urgent_count": sum(1 for t in all_t if t["urgency"] == "urgent"),
            "warning_count": sum(1 for t in all_t if t["urgency"] == "warning"),
        },
        "listings": records,
        "feed": [],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"\n✅ {OUT}")
    print(f"   {len(records)} listings, {len(all_t)} tasks, {len(own)} owners")
    for name, cnt in Counter(r["owner_name"] for r in records).most_common():
        print(f"   {name}: {cnt}")
    print(f"\n   Rules active:")
    for typ, cnt in Counter(t["type"] for t in all_t).most_common():
        print(f"     {typ}: {cnt}")

if __name__ == "__main__":
    main()
