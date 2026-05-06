#!/usr/bin/env python3
"""Regenerate data/dashboard.json using HubSpot (primary) + BigQuery (tasks).

HubSpot provides: listings, deal owners, deal stages, create dates (real-time)
BigQuery provides: agent proposals, chat messages, outbound tracking (task data)

    pip install google-cloud-bigquery requests
    gcloud auth application-default login
    export HUBSPOT_API_KEY=pat-na1-xxxxx
    python3 fetch_data.py
"""
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Install requests: pip install requests")

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None
    print("Warning: google-cloud-bigquery not installed. Tasks will be empty.", file=sys.stderr)

HUBSPOT_KEY = os.environ.get("HUBSPOT_API_KEY", "")
if not HUBSPOT_KEY:
    sys.exit("Set HUBSPOT_API_KEY environment variable (HubSpot service key)")

BQ_PROJECT = os.environ.get("BQ_PROJECT", "redy-core-platform-prod")
BQ_DATASET = os.environ.get("BQ_DATASET", "redy_prod_analytics")
OUT = Path(__file__).parent / "data" / "dashboard.json"
T = f"`{BQ_PROJECT}.{BQ_DATASET}"

HS_BASE = "https://api.hubapi.com"
HS_HEADERS = {"Authorization": f"Bearer {HUBSPOT_KEY}", "Content-Type": "application/json"}

SLA_URGENT_H = 24
SLA_WARN_H = 4

# ─── HubSpot Functions ───────────────────────────────────────────────

def hs_get(path, params=None):
    r = requests.get(f"{HS_BASE}{path}", headers=HS_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def hs_post(path, body):
    r = requests.post(f"{HS_BASE}{path}", headers=HS_HEADERS, json=body, timeout=30)
    r.raise_for_status()
    return r.json()

def hs_get_all_owners():
    owners = {}
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        data = hs_get("/crm/v3/owners", params)
        for o in data.get("results", []):
            oid = str(o["id"])
            fn, ln = o.get("firstName", ""), o.get("lastName", "")
            owners[oid] = {"id": oid, "name": f"{fn} {ln}".strip(), "initials": (fn[:1] + ln[:1]).upper()}
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
    return owners

def hs_get_deal_stages():
    stages = {}
    data = hs_get("/crm/v3/pipelines/deals")
    for pipeline in data.get("results", []):
        for stage in pipeline.get("stages", []):
            stages[stage["id"]] = stage.get("label", stage["id"])
    return stages

def hs_search_all_deals(date_from_ms=None):
    all_deals = []
    after = None
    body_base = {
        "properties": ["dealname", "dealstage", "hubspot_owner_id", "createdate", "amount"],
        "limit": 100,
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
    }
    if date_from_ms:
        body_base["filterGroups"] = [{"filters": [
            {"propertyName": "createdate", "operator": "GTE", "value": str(date_from_ms)}
        ]}]

    while True:
        body = {**body_base}
        if after:
            body["after"] = after
        data = hs_post("/crm/v3/objects/deals/search", body)
        results = data.get("results", [])
        all_deals.extend(results)
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after or not results:
            break
        time.sleep(0.1)
    return all_deals

def parse_deal(deal, owners, stages):
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
    stage_label = stages.get(stage_id, stage_id)
    # Normalize stage label to status key
    label_lower = stage_label.lower().strip()
    stage_map = {
        "proposal review": "bid_review", "bid review": "bid_review",
        "active": "active", "new": "active", "new lead": "pending_approval",
        "pending approval": "pending_approval", "agent offered": "agent_offered",
        "completed": "completed", "cancelled": "cancelled", "canceled": "cancelled",
        "rejected": "rejected", "saved": "saved", "aged lead": "aged_lead",
    }
    status = stage_map.get(label_lower, label_lower.replace(" ", "_"))

    owner_id = str(props.get("hubspot_owner_id", ""))
    owner = owners.get(owner_id, {"name": "Unassigned", "initials": "—"})
    created = props.get("createdate", "")
    created_date = created[:10] if created else ""
    price = 0
    try:
        price = int(float(props.get("amount", 0) or 0))
    except (ValueError, TypeError):
        pass

    return {
        "hubspot_deal_id": str(deal["id"]),
        "listing_id": str(deal["id"]),
        "address": address, "city": city, "state": state, "zip": zipcode,
        "price": price, "status": status, "created_date": created_date,
        "owner_id": owner_id, "owner_name": owner.get("name", "Unassigned"),
        "owner_initials": owner.get("initials", "—"),
        "seller_name": "", "tasks": [], "last_activity_label": "",
    }

# ─── BigQuery Functions ───────────────────────────────────────────────

def get_bq_task_data():
    if not bigquery:
        return {}, {}, set(), {}
    bq = bigquery.Client(project=BQ_PROJECT)

    print("  BigQuery: bids...")
    bids = {}
    for r in bq.query(f"""
        SELECT l.hubspot_deal_id,
          COUNTIF(NOT COALESCE(b.viewed,FALSE) AND b.status='placed') AS unviewed,
          STRING_AGG(DISTINCT CONCAT(u.first_name,' ',LEFT(u.last_name,1),'.') 
            ORDER BY CONCAT(u.first_name,' ',LEFT(u.last_name,1),'.') LIMIT 4) AS agent_names
        FROM {T}.stg_bids` b
        LEFT JOIN {T}.stg_users` u ON b.agent_id=u.user_id
        LEFT JOIN {T}.stg_listings` l ON b.listing_id=l.listing_id
        WHERE LOWER(b.status) NOT IN ('withdrawn','cancel','cancelled')
          AND l.hubspot_deal_id IS NOT NULL
        GROUP BY l.hubspot_deal_id
    """).result():
        if r.hubspot_deal_id:
            bids[str(r.hubspot_deal_id)] = {"unviewed": r.unviewed or 0, "agent_names": r.agent_names or ""}

    print("  BigQuery: messages...")
    msgs = {}
    for r in bq.query(f"""
        SELECT l.hubspot_deal_id,
          COUNTIF(u.role='seller' AND m.read_at IS NULL) AS unread_seller,
          COUNTIF(u.role='agent' AND m.read_at IS NULL) AS unread_agent
        FROM {T}.stg_chat_messages` m
        LEFT JOIN {T}.stg_users` u ON m.sender_id=u.user_id
        LEFT JOIN {T}.stg_bids` b ON m.bid_id=b.bid_id
        LEFT JOIN {T}.stg_listings` l ON b.listing_id=l.listing_id
        WHERE m.read_at IS NULL AND l.hubspot_deal_id IS NOT NULL
        GROUP BY l.hubspot_deal_id
    """).result():
        if r.hubspot_deal_id:
            msgs[str(r.hubspot_deal_id)] = {"unread_seller": r.unread_seller or 0, "unread_agent": r.unread_agent or 0}

    print("  BigQuery: outbound...")
    outbound = set()
    for r in bq.query(f"""
        SELECT DISTINCT CAST(l.hubspot_deal_id AS STRING) AS did
        FROM {T}.stg_chat_messages` m
        JOIN {T}.stg_bids` b ON m.bid_id=b.bid_id
        JOIN {T}.stg_listings` l ON b.listing_id=l.listing_id
        WHERE m.receiver_id=l.owner_id AND l.hubspot_deal_id IS NOT NULL
    """).result():
        outbound.add(r.did)

    print("  BigQuery: sellers...")
    sellers = {}
    for r in bq.query(f"""
        SELECT CAST(l.hubspot_deal_id AS STRING) AS did,
          CONCAT(COALESCE(u.first_name,''),' ',LEFT(COALESCE(u.last_name,''),1),'.') AS seller_name
        FROM {T}.stg_listings` l
        LEFT JOIN {T}.stg_users` u ON l.owner_id=u.user_id
        WHERE l.hubspot_deal_id IS NOT NULL
    """).result():
        if r.did and r.seller_name:
            sellers[r.did] = r.seller_name.strip()

    return bids, msgs, outbound, sellers

def build_tasks(record, bids, msgs, outbound):
    did = record["hubspot_deal_id"]
    seller = record["seller_name"] or "Seller"
    tasks = []
    b = bids.get(did, {})
    m = msgs.get(did, {})
    uv, us, ua = b.get("unviewed", 0), m.get("unread_seller", 0), m.get("unread_agent", 0)
    has_ob = did in outbound

    if us > 0:
        tasks.append({"type": "reply_seller", "urgency": "urgent", "icon": "📞",
            "text": f"Reply to {seller} (seller) — {us} unread message{'s'*(us!=1)}", "age_label": "urgent", "age_hours": 25})
    if ua > 0:
        tasks.append({"type": "reply_agent", "urgency": "urgent", "icon": "💬",
            "text": f"Reply to {ua} unread agent message{'s'*(ua!=1)}", "age_label": "urgent", "age_hours": 25})
    if uv > 0 and not has_ob:
        tasks.append({"type": "call_seller", "urgency": "warning", "icon": "📞",
            "text": f"Call {seller} (seller) — {uv} proposal{'s'*(uv!=1)} received, no outbound contact yet", "age_label": "warning", "age_hours": 12})
    if uv > 0:
        agents = b.get("agent_names", "")
        tasks.append({"type": "new_proposals", "urgency": "info", "icon": "📄",
            "text": f"{uv} new proposal{'s'*(uv!=1)} from agents — notify seller {seller}" + (f" ({agents})" if agents else ""), "age_label": "today", "age_hours": 0})

    tasks.sort(key=lambda t: ({"urgent": 0, "warning": 1, "info": 2}.get(t["urgency"], 9), -t.get("age_hours", 0)))
    return tasks

# ─── Main ─────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)

    print("1. Fetching HubSpot owners...")
    owners = hs_get_all_owners()
    print(f"   {len(owners)} owners")

    print("2. Fetching deal stages...")
    stages = hs_get_deal_stages()
    for sid, label in stages.items():
        print(f"   {sid}: {label}")

    print("3. Fetching deals from HubSpot (last 90 days)...")
    cutoff_ms = int((now.timestamp() - 90 * 86400) * 1000)
    deals = hs_search_all_deals(date_from_ms=cutoff_ms)
    print(f"   {len(deals)} deals")

    print("4. Parsing deals...")
    records = [parse_deal(d, owners, stages) for d in deals]
    print(f"   {len(records)} listings")

    print("5. Enriching with BigQuery task data...")
    bids, msgs, outbound, sellers = get_bq_task_data()
    print(f"   {len(bids)} with bids, {len(msgs)} with messages, {len(sellers)} seller names")

    for r in records:
        did = r["hubspot_deal_id"]
        if did in sellers:
            r["seller_name"] = sellers[did]
        r["tasks"] = build_tasks(r, bids, msgs, outbound)

    records.sort(key=lambda r: (
        min(({"urgent": 0, "warning": 1, "info": 2}.get(t["urgency"], 9) for t in r["tasks"]), default=3),
        -max((t.get("age_hours", 0) for t in r["tasks"]), default=0)))

    own = {}
    for r in records:
        if r["owner_id"] and r["owner_id"] not in own:
            own[r["owner_id"]] = {"id": r["owner_id"], "name": r["owner_name"], "initials": r["owner_initials"]}

    all_t = [t for r in records for t in r["tasks"]]
    payload = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "HubSpot (primary) + BigQuery (tasks)",
        "owners": sorted(own.values(), key=lambda o: o["name"]),
        "statuses": sorted({r["status"] for r in records if r["status"]}),
        "kpis": {"total_listings": len(records), "total_tasks": len(all_t),
                 "urgent_count": sum(1 for t in all_t if t["urgency"] == "urgent"),
                 "warning_count": sum(1 for t in all_t if t["urgency"] == "warning")},
        "listings": records, "feed": [],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"\nDone! {OUT}")
    print(f"  {len(records)} listings, {len(all_t)} tasks, {len(own)} owners")
    from collections import Counter
    for name, cnt in Counter(r["owner_name"] for r in records).most_common():
        print(f"  {name}: {cnt}")

if __name__ == "__main__":
    main()
