Redy Dashboard

A static dashboard showing agent proposals received and seller/agent chat
message notifications.

## Run

Open `index.html` directly, or serve the folder:

    python3 -m http.server 8000
    # then visit http://localhost:8000

## Files

- `index.html` — layout (KPI cards, trend chart, notifications feed)
- `styles.css` — styling
- `app.js`    — chart rendering and mock data generator

To wire to a real backend, replace `loadData()` in `app.js` with a `fetch()`
returning `{ series: [{ date, proposals, seller, agent }], feed: [...] }`.
