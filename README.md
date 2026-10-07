# QRDock — dynamic QR codes, self-hosted

QR codes that stay editable **after** you print them. Change the target URL
anytime — no reprint, no QR service subscription, no account wall.

- Redirect QR codes (`/q/<slug>` → 302 to current target) with scan counts
- PNG + SVG download of every code
- Instant anonymous QR (`/qr.png?text=...`) — zero signup
- Python + FastAPI + SQLite, ~50 MB RAM, one `docker run`
- Pro extras: custom QR colors via `?fg=`, 14-day scan chart

## Deploy

```bash
docker build -t qrdock .
docker run -d -p 8000:8000 -v qrdock-data:/data qrdock
```

Open `http://localhost:8000`, register, create a link, print the QR —
retarget it forever.

Part of the [Grand Line fleet](https://github.com/seyitwb-svg/grand-line-fleet):
20 self-hosted micro-SaaS tools. QRDock is the free, MIT-licensed member —
the full source bundle sells for a one-time BTC/USDT payment.

License: MIT
