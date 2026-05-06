#!/usr/bin/env python3
"""Regenerate data/dashboard.json from BigQuery.

Pulls activity metrics and recent feed items from
`redy-core-platform-prod.redy_prod_analytics` (stg_bids, stg_chat_messages,
stg_users, stg_listings) and writes them to `data/dashboard.json`.

Requires google-cloud-bigquery and application-default credentials:
    pip install google-cloud-bigquery
    gcloud auth application-default login
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery

PROJECT = os.environ.get("BQ_PROJECT", "redy-core-platform-prod")
DATASET = os.environ.get("BQ_DATASET", "redy_prod_analytics")
DAYS = int(os.environ.get("BQ_DAYS", "60"))
FEED_LIMIT = int(os.environ.get("FEED_LIMIT", "50"))

OUT = Path(__file__).parent / "data" / "dashboard.json"

SERIES_SQL = f"""
WITH dates AS (
  SELECT date FROM UNNEST(GENERATE_DATE_ARRAY(
    DATE_SUB(CURRENT_DATE(), INTERVAL @days - 1 DAY), CURRENT_DATE())) date
),
bids_d AS (
  SELECT DATE(created_at) d,
         COUNT(*) proposals,
         COUNTIF(NOT COALESCE(viewed, FALSE)) unread_bids
  FROM `{PROJECT}.{DATASET}.stg_bids`
  WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days - 1 DAY)
  GROUP BY d
),
chat_d AS (
  SELECT DATE(m.created_at) d,
         COUNTIF(u.role = 'seller') seller,
         COUNTIF(u.role = 'agent')  agent,
         COUNTIF(m.read_at IS NULL) unread_msgs
  FROM `{PROJECT}.{DATASET}.stg_chat_messages` m
  LEFT JOIN `{PROJECT}.{DATASET}.stg_users` u ON u.user_id = m.sender_id
  WHERE DATE(m.created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days - 1 DAY)
  GROUP BY d
)
SELECT FORMAT_DATE('%Y-%m-%d', d.date) date,
       COALESCE(b.proposals, 0)   proposals,
       COALESCE(c.seller, 0)      seller,
       COALESCE(c.agent, 0)       agent,
       COALESCE(b.unread_bids, 0) unread_bids,
       COALESCE(c.unread_msgs, 0) unread_msgs
FROM dates d
LEFT JOIN bids_d b ON b.d = d.date
LEFT JOIN chat_d c ON c.d = d.date
ORDER BY d.date
"""

ADDR_EXPR = """
COALESCE(
  NULLIF(TRIM(CONCAT(
    COALESCE(l.address_street, ''),
    IF(l.address_city  IS NOT NULL, CONCAT(', ', l.address_city),  ''),
    IF(l.address_state IS NOT NULL, CONCAT(', ', l.address_state), '')
  )), ''),
  CONCAT('listing ', SUBSTR(COALESCE(l.listing_id, ''), 1, 8))
)
"""

FEED_SQL = f"""
SELECT * FROM (
  SELECT
    'proposal' AS kind,
    b.created_at AS when_ts,
    CONCAT(COALESCE(u.first_name, 'Agent'), ' ', COALESCE(u.last_name, '')) AS who,
    {ADDR_EXPR} AS addr,
    NOT COALESCE(b.viewed, FALSE) AS unread
  FROM `{PROJECT}.{DATASET}.stg_bids` b
  LEFT JOIN `{PROJECT}.{DATASET}.stg_users`    u ON u.user_id    = b.agent_id
  LEFT JOIN `{PROJECT}.{DATASET}.stg_listings` l ON l.listing_id = b.listing_id
  WHERE DATE(b.created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days - 1 DAY)
  ORDER BY b.created_at DESC
  LIMIT @feed_limit
)
UNION ALL
SELECT * FROM (
  SELECT
    CASE WHEN u.role = 'agent' THEN 'agent' ELSE 'seller' END AS kind,
    m.created_at AS when_ts,
    CONCAT(COALESCE(u.first_name, ''), ' ', COALESCE(u.last_name, '')) AS who,
    {ADDR_EXPR} AS addr,
    m.read_at IS NULL AS unread
  FROM `{PROJECT}.{DATASET}.stg_chat_messages` m
  LEFT JOIN `{PROJECT}.{DATASET}.stg_users`    u ON u.user_id    = m.sender_id
  LEFT JOIN `{PROJECT}.{DATASET}.stg_bids`     b ON b.bid_id     = m.bid_id
  LEFT JOIN `{PROJECT}.{DATASET}.stg_listings` l ON l.listing_id = b.listing_id
  WHERE DATE(m.created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days - 1 DAY)
    AND u.role IN ('agent', 'seller')
  ORDER BY m.created_at DESC
  LIMIT @feed_limit
)
ORDER BY when_ts DESC
LIMIT @feed_limit
"""


def main():
    client = bigquery.Client(project=PROJECT)

    params = [
        bigquery.ScalarQueryParameter("days", "INT64", DAYS),
        bigquery.ScalarQueryParameter("feed_limit", "INT64", FEED_LIMIT),
    ]
    cfg = bigquery.QueryJobConfig(query_parameters=params)

    series = [dict(row) for row in client.query(SERIES_SQL, job_config=cfg).result()]
    feed_rows = list(client.query(FEED_SQL, job_config=cfg).result())
    feed = [
        {
            "kind": r["kind"],
            "who": (r["who"] or "").strip(),
            "addr": r["addr"],
            "when": r["when_ts"].timestamp(),
            "unread": bool(r["unread"]),
        }
        for r in feed_rows
    ]

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": f"{PROJECT}.{DATASET}",
        "series": series,
        "feed": feed,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {OUT} ({len(series)} days, {len(feed)} feed items)")


if __name__ == "__main__":
    main()
