Redy Dashboard

A static dashboard showing agent proposals received and seller/agent chat
message notifications. Data is sourced from BigQuery
(`redy-core-platform-prod.redy_prod_analytics`: `stg_bids`,
`stg_chat_messages`, `stg_users`, `stg_listings`).

## Run

Serve the folder so the browser can `fetch()` `data/dashboard.json`:

    python3 -m http.server 8000
    # then visit http://localhost:8000

## Files

- `index.html`           — layout (KPI cards, trend chart, notifications feed)
- `styles.css`           — styling
- `app.js`               — fetches `data/dashboard.json` and renders the UI
- `fetch_data.py`        — refreshes `data/dashboard.json` from BigQuery
- `data/dashboard.json`  — pre-generated metrics used by the dashboard

## Refreshing data

Requires the BigQuery client and application-default credentials with read
access to `redy-core-platform-prod`:

    pip install google-cloud-bigquery
    gcloud auth application-default login
    python3 fetch_data.py

The script writes a 60-day series and the 50 most recent feed items to
`data/dashboard.json`. Configurable via env vars: `BQ_PROJECT`, `BQ_DATASET`,
`BQ_DAYS`, `FEED_LIMIT`.
