#!/usr/bin/env python3
"""Regenerate data/dashboard.json from BigQuery + HubSpot.

    pip install google-cloud-bigquery requests
    gcloud auth application-default login
    export HUBSPOT_API_KEY=pat-na1-xxxxx
    python3 fetch_data.py
"""
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
from google.cloud import bigquery
try:
    import requests
except ImportError:
    requests = None

PROJECT = os.environ.get("BQ_PROJECT", "redy-core-platform-prod")
DATASET = os.environ.get("BQ_DATASET", "redy_prod_analytics")
HUBSPOT_KEY = os.environ.get("HUBSPOT_API_KEY", "")
OUT = Path(__file__).parent / "data" / "dashboard.json"
SLA_URGENT_H = 24
SLA_WARN_H = 4
STALLED_DAYS = 7
T = f"`{PROJECT}.{DATASET}"

LISTINGS_Q = f"""
SELECT l.listing_id, l.hubspot_deal_id, l.owner_id seller_uid, l.status,
  l.address_street, l.address_city, l.address_state, l.address_zip, l.price,
  l.created_at listing_ts, l.updated_at update_ts,
  COALESCE(u.first_name,'') sfirst, COALESCE(u.last_name,'') slast
FROM {T}.stg_listings` l
LEFT JOIN {T}.stg_users` u ON l.owner_id=u.user_id
WHERE l.status NOT IN ('deleted')
  AND l.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
ORDER BY l.created_at DESC
"""

BIDS_Q = f"""
SELECT b.bid_id, b.listing_id, b.agent_id, b.amount, b.bml_commission,
  b.status, COALESCE(b.viewed,FALSE) viewed, b.created_at,
  COALESCE(u.first_name,'') afirst, COALESCE(u.last_name,'') alast
FROM {T}.stg_bids` b
LEFT JOIN {T}.stg_users` u ON b.agent_id=u.user_id
WHERE b.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
  AND LOWER(b.status) NOT IN ('withdrawn','cancel','cancelled')
"""

MSGS_Q = f"""
SELECT m.message_id, m.sender_id, m.receiver_id, m.bid_id, m.read_at,
  m.created_at, COALESCE(u.role,'') role,
  COALESCE(u.first_name,'') mfirst, COALESCE(u.last_name,'') mlast,
  b.listing_id, LEFT(m.message, 80) AS preview
FROM {T}.stg_chat_messages` m
LEFT JOIN {T}.stg_users` u ON m.sender_id=u.user_id
LEFT JOIN {T}.stg_bids` b ON m.bid_id=b.bid_id
WHERE m.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
"""

FEED_Q = f"""
SELECT * FROM (
  SELECT 'proposal' kind, b.created_at ts,
    CONCAT(COALESCE(u.first_name,''),' ',COALESCE(u.last_name,'')) who,
    COALESCE(l.address_street,'') addr,
    CONCAT(COALESCE(l.address_city,''),', ',COALESCE(l.address_state,'')) loc,
    NOT COALESCE(b.viewed,FALSE) unread, l.listing_id
  FROM {T}.stg_bids` b
  LEFT JOIN {T}.stg_users` u ON u.user_id=b.agent_id
  LEFT JOIN {T}.stg_listings` l ON l.listing_id=b.listing_id
  WHERE b.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  ORDER BY b.created_at DESC LIMIT 50
) UNION ALL SELECT * FROM (
  SELECT CASE WHEN u.role='agent' THEN 'agent' ELSE 'seller' END kind,
    m.created_at ts, CONCAT(COALESCE(u.first_name,''),' ',COALESCE(u.last_name,'')) who,
    COALESCE(l.address_street,'') addr,
    CONCAT(COALESCE(l.address_city,''),', ',COALESCE(l.address_state,'')) loc,
    m.read_at IS NULL unread, l.listing_id
  FROM {T}.stg_chat_messages` m
  LEFT JOIN {T}.stg_users` u ON u.user_id=m.sender_id
  LEFT JOIN {T}.stg_bids` b ON b.bid_id=m.bid_id
  LEFT JOIN {T}.stg_listings` l ON l.listing_id=b.listing_id
  WHERE m.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
    AND u.role IN ('agent','seller')
  ORDER BY m.created_at DESC LIMIT 50
) ORDER BY ts DESC LIMIT 50
"""

def hs_owners(deal_ids):
    if not HUBSPOT_KEY or not requests or not deal_ids:
        return {}
    hdr = {"Authorization": f"Bearer {HUBSPOT_KEY}", "Content-Type": "application/json"}
    out = {}
    ids = list({d for d in deal_ids if d})
    for i in range(0, len(ids), 100):
        try:
            r = requests.post("https://api.hubapi.com/crm/v3/objects/deals/batch/read",
                headers=hdr, timeout=30,
                json={"properties":["hubspot_owner_id"],"inputs":[{"id":str(d)} for d in ids[i:i+100]]})
            if r.status_code == 200:
                for x in r.json().get("results",[]):
                    oid = x.get("properties",{}).get("hubspot_owner_id")
                    if oid: out[int(x["id"])] = int(oid)
        except Exception as e:
            print(f"  HS deal error: {e}", file=sys.stderr)
    owner_ids = list(set(out.values()))
    names = {}
    for i in range(0, len(owner_ids), 100):
        try:
            for oid in owner_ids[i:i+100]:
                r = requests.get(f"https://api.hubapi.com/crm/v3/owners/{oid}", headers=hdr, timeout=15)
                if r.status_code == 200:
                    d = r.json()
                    fn = d.get("firstName",""); ln = d.get("lastName","")
                    names[oid] = {"name": f"{fn} {ln}".strip(), "initials": (fn[:1]+ln[:1]).upper()}
        except Exception as e:
            print(f"  HS owner error: {e}", file=sys.stderr)
    return out, names

def name_short(first, last):
    return f"{first} {last[0]}." if last else first

def age(h):
    if h < 1: return "just now"
    if h < 24: return f"{int(h)}h ago"
    d = int(h/24)
    return f"{d}d ago" if d < 30 else f"{int(d/30)}mo ago"

def overdue(h):
    return f"{int(h)}h overdue" if h < 24 else f"{int(h/24)}d overdue"

def gen_tasks(L, bids, msgs, now):
    tasks = []
    lid, sid = L["listing_id"], L["seller_uid"]
    seller_name = name_short(L["sfirst"], L["slast"]) if L.get("slast") else L.get("sfirst","Seller")
    lb = [b for b in bids if b["listing_id"]==lid]
    lm = [m for m in msgs if m.get("listing_id")==lid]
    placed = [b for b in lb if b["status"]=="placed"]
    us = [m for m in lm if m["role"]=="seller" and m["read_at"] is None]
    ua = [m for m in lm if m["role"]=="agent" and m["read_at"] is None]
    outbound = [m for m in lm if m.get("receiver_id")==sid]

    # Urgent: unread seller messages
    for m in us:
        h = (now - m["created_at"]).total_seconds()/3600
        preview = (m.get("preview","") or "")[:60]
        u = "urgent" if h>SLA_URGENT_H else "warning" if h>SLA_WARN_H else "info"
        label = overdue(h-SLA_URGENT_H) if u=="urgent" else age(h)
        sender = name_short(m["mfirst"], m["mlast"])
        text = f'Call {sender} back — seller messaged "{preview}" {age(h)}, still unread' if preview else f"Reply to {sender} (seller) — unread message sent {age(h)}"
        tasks.append({"type":"reply_seller","urgency":u,"icon":"phone","text":text,"age_hours":round(h,1),"age_label":label})

    # Urgent/warning: unread agent messages (one per agent)
    seen_agents = set()
    for m in sorted(ua, key=lambda x: x["created_at"]):
        agent_name = f"{m['mfirst']} {m['mlast']}".strip()
        if agent_name in seen_agents: continue
        seen_agents.add(agent_name)
        h = (now - m["created_at"]).total_seconds()/3600
        u = "urgent" if h>SLA_URGENT_H else "warning" if h>SLA_WARN_H else "info"
        label = overdue(h-SLA_URGENT_H) if u=="urgent" else age(h)
        tasks.append({"type":"reply_agent","urgency":u,"icon":"message-2",
            "text":f"Reply to {agent_name} (agent) — unread message sent {age(h)}",
            "age_hours":round(h,1),"age_label":label})

    # Warning: proposals waiting, no seller contact
    if placed and not outbound:
        h = (now - min(b["created_at"] for b in placed)).total_seconds()/3600
        n = len(placed)
        u = "urgent" if h>SLA_URGENT_H else "warning" if h>SLA_WARN_H else "info"
        label = overdue(h-SLA_URGENT_H) if u=="urgent" else age(h)
        tasks.append({"type":"call_seller","urgency":u,"icon":"phone",
            "text":f"Call {seller_name} (seller) — {n} proposal{'s'*(n!=1)} received but no outbound contact yet",
            "age_hours":round(h,1),"age_label":label})

    # Info: new proposals to notify seller about
    if placed:
        h = (now - max(b["created_at"] for b in placed)).total_seconds()/3600
        names = ", ".join(name_short(b["afirst"],b["alast"]) for b in placed[:4])
        n = len(placed)
        tasks.append({"type":"new_proposals","urgency":"info","icon":"file-text",
            "text":f"{n} new proposal{'s'*(n!=1)} from agents — notify seller {seller_name}" + (f" ({names})" if names else ""),
            "age_hours":round(h,1),"age_label":"today" if h<24 else age(h)})

    # Warning: stalled listing
    sh = (now - L["update_ts"]).total_seconds()/3600
    if sh > STALLED_DAYS*24 and L["status"] not in ("completed","deleted","cancelled","rejected"):
        tasks.append({"type":"stalled","urgency":"warning","icon":"alert-triangle",
            "text":f"Listing stalled — no activity in {int(sh/24)} days",
            "age_hours":round(sh,1),"age_label":f"{int(sh/24)}d"})

    order = {"urgent":0,"warning":1,"info":2}
    tasks.sort(key=lambda t:(order.get(t["urgency"],9),-t["age_hours"]))
    return tasks

def main():
    now = datetime.now(timezone.utc)
    bq = bigquery.Client(project=PROJECT)
    print("Fetching listings..."); listings = [dict(r) for r in bq.query(LISTINGS_Q).result()]
    print(f"  {len(listings)} listings")
    print("Fetching bids..."); bids = [dict(r) for r in bq.query(BIDS_Q).result()]
    print(f"  {len(bids)} bids")
    print("Fetching messages..."); msgs = [dict(r) for r in bq.query(MSGS_Q).result()]
    print(f"  {len(msgs)} messages")
    print("Fetching feed..."); feed_rows = [dict(r) for r in bq.query(FEED_Q).result()]

    dids = [l["hubspot_deal_id"] for l in listings if l.get("hubspot_deal_id")]
    print(f"Fetching HubSpot owners ({len(dids)} deals)...")
    deal_map, owner_names = hs_owners(dids)
    print(f"  Mapped {len(deal_map)} deals to {len(owner_names)} owners")

    records = []
    for l in listings:
        oid = deal_map.get(l.get("hubspot_deal_id"))
        oinfo = owner_names.get(oid, {"name":"Unassigned","initials":"—"})
        tasks = gen_tasks(l, bids, msgs, now)
        sname = name_short(l["sfirst"],l["slast"]) if l.get("slast") else l.get("sfirst","")
        lmsgs = [m for m in msgs if m.get("listing_id")==l["listing_id"]]
        acts = ([b["created_at"] for b in bids if b["listing_id"]==l["listing_id"]] +
                [m["created_at"] for m in lmsgs] + [l["listing_ts"]])
        lah = (now - max(acts)).total_seconds()/3600

        records.append({
            "listing_id":l["listing_id"], "address":l.get("address_street",""),
            "city":l.get("address_city",""), "state":l.get("address_state",""),
            "zip":l.get("address_zip",""), "price":l.get("price") or 0,
            "status":l.get("status",""),
            "created_date": l["listing_ts"].strftime("%Y-%m-%d") if l.get("listing_ts") else "",
            "owner_id":str(oid) if oid else "",
            "owner_name":oinfo["name"], "owner_initials":oinfo["initials"],
            "seller_name":sname.strip(),
            "tasks":tasks,
            "last_activity_hours":round(lah,1), "last_activity_label":age(lah),
        })

    urank = {"urgent":0,"warning":1,"info":2}
    records.sort(key=lambda r:(min((urank.get(t["urgency"],9) for t in r["tasks"]),default=3),
        -max((t["age_hours"] for t in r["tasks"]),default=0)))

    owners = {}
    for r in records:
        if r["owner_id"] and r["owner_id"] not in owners:
            owners[r["owner_id"]] = {"id":r["owner_id"],"name":r["owner_name"],"initials":r["owner_initials"]}
    owners_list = sorted(owners.values(), key=lambda o:o["name"])

    all_t = [t for r in records for t in r["tasks"]]
    feed = [{"kind":r["kind"],"who":(r.get("who") or "").strip(),"addr":r.get("addr",""),
        "loc":r.get("loc",""),"when":r["ts"].timestamp(),"unread":bool(r.get("unread")),
        "listing_id":r.get("listing_id","")} for r in feed_rows]

    payload = {
        "generated_at":now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source":f"{PROJECT}.{DATASET}",
        "owners":owners_list,
        "statuses":sorted({r["status"] for r in records if r["status"]}),
        "kpis":{"total_listings":len(records),"total_tasks":len(all_t),
            "urgent_count":sum(1 for t in all_t if t["urgency"]=="urgent"),
            "warning_count":sum(1 for t in all_t if t["urgency"]=="warning")},
        "listings":records, "feed":feed,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT,"w") as f: json.dump(payload, f, indent=2, default=str)
    print(f"\nWrote {OUT} ({len(records)} listings, {len(all_t)} tasks, {len(feed)} feed)")

if __name__ == "__main__":
    main()
