# jan-telemetry

Cloudflare Python Worker + D1: `/ingest` for Log.luau batches, `/api/*` read routes, the
dashboard at `/`, and a once-a-minute cron. Spec: "Real-Time Playtime Telemetry", Sep 27 2026.

## Local

```bash
uv tool install workers-py                      # provides pywrangler
printf 'INGEST_TOKEN=local-dev-token\nDEV_NO_AUTH=1\n' > .dev.vars
npx wrangler d1 migrations apply DB --local
pywrangler dev --port 8787
./test/smoke.sh                                 # 401/400/204 checks + every read route
uv run --no-project --with pytest pytest -q test
```

Cron locally:

```bash
curl -X POST "localhost:8787/cdn-cgi/local/explorer/api/local/scheduled?worker=jan-telemetry" \
  -H 'content-type: application/json' -d '{"cron":"* * * * *"}'
```

Fake history for looking at the dashboard: `python3 test/seed_local.py > /tmp/seed.sql && npx wrangler d1 execute DB --local --file /tmp/seed.sql`.

## Deploy

```bash
npx wrangler login
npx wrangler d1 create jan-telemetry           # paste database_id into wrangler.toml
npx wrangler d1 migrations apply DB --remote
(umask 077; mkdir -p ~/.config/jan-telemetry; openssl rand -hex 32 | tr -d "\n" > ~/.config/jan-telemetry/ingest_token)
cat ~/.config/jan-telemetry/ingest_token | npx wrangler secret put INGEST_TOKEN
pywrangler deploy
./test/smoke.sh https://jan-telemetry.dhanushnagesh.workers.dev "$(cat ~/.config/jan-telemetry/ingest_token)"   # /api/* will 403 until Access is set
```

Then in the Cloudflare dashboard, Zero Trust → Access → Applications: add a self-hosted app
for the Worker's hostname covering `/`, with bypass policies for `/ingest` and `/badge/*`. Copy the app's AUD
tag and team domain into `ACCESS_AUD` and `ACCESS_TEAM_DOMAIN` in wrangler.toml and redeploy.
The Worker verifies the Access JWT itself, so `/api/*` stays closed even on a hostname Access
does not cover.

Game side: set `ServerStorage.TelemetryEndpoint` to `https://…/ingest` and
`ServerStorage.TelemetryToken` to the same token.

## README badges

`/badge/ccu` and `/badge/playtime` are public and return Shields.io endpoint JSON. Each Worker
isolate caches them for 60 s, and Shields caches for 300 s on top of that.

## Budget (free tier)

One POST per server per Log flush. At `FLUSH_INTERVAL = 10` that is 8,640 requests a day per
server, so the 100,000/day Workers limit is reached at about 10 concurrent servers, before
D1's write limit. At `FLUSH_INTERVAL = 30`, about 30 servers fit.
