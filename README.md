# RBX Analytics

dashboard is private (behind Cloudflare Access)

![Players in game right now](https://img.shields.io/endpoint?url=https%3A%2F%2Fjan-telemetry.dhanushnagesh.workers.dev%2Fbadge%2Fccu&cacheSeconds=300)
![Average session length today](https://img.shields.io/endpoint?url=https%3A%2F%2Fjan-telemetry.dhanushnagesh.workers.dev%2Fbadge%2Fplaytime&cacheSeconds=300)

*These two badges come from the live pipeline. They can be up to about 5 minutes old because
Shields.io and GitHub both cache them.*

JBI Analytics is the
telemetry pipeline I built for my game: the game servers send session, round and presence events
to a Cloudflare Worker, the Worker stores them in a D1 (SQLite) database, and a small
dashboard shows live players, session lengths and round stats.

## Why I built this

Roblox's creator dashboard gives you daily totals like visits, average playtime and
concurrent users, but it doesn't let you look at individual sessions or query the raw data. 
Also data can be 1-2 days behind due to Roblox's quality assurance with player data. 
I wanted to answer questions like "how long does a session last if you take out AFK time?",
"do people leave after their first round?" and "which way do rounds usually end?", and to
see whether a game update actually changed any of that. None of those are answerable from
the built-in charts, so I started logging the events myself.

## How it works

```
ROBLOX GAME SERVER (one per match server)

  PlayerAdded / PlayerRemoving ─┐
  GameLoop round start/end ─────┼──► Log.luau ──── buffers events in memory
  presence snapshot (players) ──┘        │
                                         │  HttpService POST /ingest every 10 s
                                         │  JSON array, up to 500 events,
                                         │  Authorization: Bearer <token>
                                         ▼
CLOUDFLARE                        ┌──────────────────┐
                                  │  Worker (Python) │
                                  │  /ingest         │
                                  └──────────────────┘
                                         │  one D1 batch per request
                                         │  (all-or-nothing transaction)
                                         ▼
   Cron Trigger, every minute ───► ┌──────────────────┐
   closes dead servers/sessions,   │   D1 (SQLite)    │
   writes a CCU row, and once a    │ sessions, rounds │
   day deletes old data            │ servers, events, │
                                   │ ccu_minute       │
                                   └──────────────────┘
                                         │  read-only SQL, aggregated per request
                                         ▼
                                  ┌──────────────────┐   JSON, polled every 15 s
                                  │ Worker /api/*    │ ─────────────────────────┐
                                  └──────────────────┘                          ▼
                                   Cloudflare Access (login + JWT check) ─► dashboard
                                                                         (static HTML,
                                                                          same Worker)
```

1. **Game side.** A server script (`Log.luau`) collects events from the rest of the game
   and POSTs them to the Worker in batches. I used in-game `HttpService` calls instead of
   Open Cloud because the data starts inside the game server anyway, and Open Cloud is for
   calling Roblox's APIs from outside.
2. **Ingest.** `/ingest` checks the bearer token, parses the batch, and turns each event
   into SQL. The whole batch goes into D1 as one transaction.
3. **Cron.** Once a minute, the Worker marks servers closed if they stopped sending
   presence, closes their open sessions, and records the current CCU. Once a day it deletes
   old rows.
4. **Read API.** `/api/live`, `/api/summary`, `/api/ccu`, `/api/top` and `/api/rounds`
   run SQL against D1 and return JSON. They only work with a valid Cloudflare Access JWT.
5. **Dashboard.** One HTML page served by the same Worker. It calls the API and draws the
   charts itself.
6. **README badges.** `/badge/ccu` and `/badge/playtime` are the only public read routes.
   They return two numbers in the JSON format Shields.io expects, and Shields turns that
   into the badges at the top of this page.

## Tech stack

| Part | What I used |
|---|---|
| Game code | Luau, `HttpService:PostAsync` from a server script |
| Event collection | `Log.luau` (my own module in the game, buffers and batches events) |
| Backend | Cloudflare **Workers**, written in Python (Pyodide / Python Workers) |
| Database | Cloudflare **D1** |
| Scheduled jobs | Workers **Cron Triggers** (`* * * * *`) |
| Dashboard hosting | Workers **static assets**, served by the same Worker |
| Dashboard | One HTML file, vanilla JS, charts drawn as SVG by hand, no framework |
| Auth | Bearer token for `/ingest`, Cloudflare **Access** for the dashboard and `/api/*` |
| Tooling | `pywrangler` / `wrangler`, uv, pytest (14 tests run the real SQL against sqlite3) |

I don't use KV or R2. Everything is small enough to just live in D1.

## What it measures

What's working now:

- **Concurrent players**, per minute, with the same time yesterday overlaid (the current
  number is also the "playing now" badge at the top)
- **Live servers and players**, including what each player is doing right now (lobby,
  hider, seeker, jailed, practice, AFK)
- **Session length**: average, median, and a histogram (<1, 1-5, 5-15, 15-30, 30-60, 60+ min).
  Today's average is also a badge at the top. It only counts sessions that started since
  midnight UTC and have already ended, so right after midnight it's based on very few sessions.
- **AFK share**: how much of total session time was spent AFK
- **Playtime hours and unique players** for any time range
- **Top players** by playtime, with sessions and rounds played
- **Rounds**: rounds started per hour, how rounds end, and which side wins

Each session also stores AFK, practice and in-round seconds, rounds played, account age
and whether the player joined by following a friend. None of those have their own chart yet
but they're in the table.

What's only partly there:

- **D1 / D7 retention.** The data is there and the query is in
  [`telemetry/queries/retention.sql`](telemetry/queries/retention.sql), but it isn't in the
  API or the dashboard yet. I run it by hand.

What I don't track:

- **Revenue.** No purchase events are logged, so I have nothing on Robux spent, game passes
  or developer products. For now revenue only comes from Roblox's own dashboard.
- **Funnels.** There's no step-by-step funnel (join → first round → second round → ...).
  I could build a rough one from `sessions.rounds`, but I haven't.
- **Where players come from** (search, home page, sponsored), and device type.

## Data model

The game sends a flat JSON array of events. Every event has `event`, `level`, `at` (unix
seconds, from the game server's clock), `jobId` (the Roblox server ID) and `placeVersion`.
This is what a `session_end` looks like:

```json
{
  "event": "session_end", "level": "INFO", "at": 1790001800,
  "jobId": "5c1e...", "placeVersion": 42,
  "userId": 123456789, "sessionId": "b7f2...",
  "joinedAt": 1790000000, "leftAt": 1790001800, "seconds": 1800,
  "afkSeconds": 120, "practiceSeconds": 60, "roundSeconds": 1400,
  "rounds": 5, "reason": "left"
}
```

The Worker doesn't just append events. It sorts them into tables:

| Table | One row per | Filled by |
|---|---|---|
| `sessions` | player session (one join to one leave) | `session_start` inserts, `session_end` fills in the end |
| `rounds` | round, keyed `jobId:roundId` | `round_start` / `round_end` |
| `servers` | game server, with its latest player list | `presence`, `server_close` |
| `ccu_minute` | minute | the cron, summing players on open public servers |
| `events` | any other event, stored raw | everything except the above (and `shot`, which fires too often) |

A few things I had to think about:

- **Replays are safe.** If a POST times out, `Log.luau` might send the same batch again.
  Sessions and rounds use `ON CONFLICT` upserts, and raw events are keyed by a hash of their
  JSON, so sending a batch twice does nothing.
- **Lost `session_end`s.** If a server crashes, the player's `session_end` never arrives.
  Every presence snapshot lists who's on the server, so if an open session is missing from
  it, the Worker closes it at the previous snapshot and marks it `timeout`. The cron does
  the same for servers that stop sending presence for 90 seconds.
- **Lost `session_start`s.** If a player shows up in a snapshot with no session row, the
  Worker creates one from the snapshot.
- **Private and Studio servers** are flagged on every row and filtered out by default, so
  me testing in Studio doesn't count as players.

### From rows to a metric: D1 retention

Retention comes straight from `sessions`. First I reduce it to distinct (player, day)
pairs, then find each player's first day, then check if they came back exactly 1 and 7 days
later:

```sql
WITH days AS (
  SELECT DISTINCT user_id, joined_at / 86400 AS day      -- UTC day number
  FROM sessions
  WHERE private = 0 AND studio = 0
),
cohort AS (
  SELECT user_id, MIN(day) AS first_day FROM days GROUP BY user_id
)
SELECT
  date(c.first_day * 86400, 'unixepoch') AS cohort_day,
  COUNT(*) AS new_players,
  ROUND(100.0 * SUM(EXISTS (SELECT 1 FROM days d
        WHERE d.user_id = c.user_id AND d.day = c.first_day + 1)) / COUNT(*), 1) AS d1_pct,
  ROUND(100.0 * SUM(EXISTS (SELECT 1 FROM days d
        WHERE d.user_id = c.user_id AND d.day = c.first_day + 7)) / COUNT(*), 1) AS d7_pct
FROM cohort c
GROUP BY c.first_day
ORDER BY c.first_day;
```

So if 40 players were first seen on Oct 1 and 12 of them had any session on Oct 2, D1 for
that cohort is 30%. This uses UTC days, so a player who plays at 11 PM and again at 1 AM
Pacific counts as coming back the next day. Roblox's own retention probably works
differently, so I wouldn't compare the two numbers directly.

Session length is simpler. The dashboard takes `seconds` from sessions that started in the
range and have ended, and computes the average, the median (sorted, `LIMIT 2 OFFSET
(n-1)/2`) and the histogram buckets with a `CASE`. Playtime hours use the overlap of each
session with the time range, so a session that crosses midnight is split between the two days.

## Limitations

- **Crashed servers cut sessions short by a bit.** A session closed by timeout ends at
  the last presence snapshot the Worker got, so it can be a little shorter than it really
  was (up to the gap between two snapshots). These rows have `end_reason = 'timeout'` if you want to exclude them.
- **Events buffered on a server that crashes** (up to 10 seconds' worth) are lost. The next
  presence snapshot fixes the session table, but other events are just gone.
- **Player IDs only.** I don't store usernames or anything besides the user ID and account
  age. There's a `DELETE /api/user/<id>` route to remove a player's data.

## Running it

This isn't really portable. It depends on my game's `Log.luau` event format, and the
Worker is set up for my Cloudflare account and Access app. If you want to run the Worker
and dashboard locally with fake data anyway, the steps are in
[`telemetry/README.md`](telemetry/README.md) (local D1, a seed script, a smoke test and the
pytest suite).

## Cost

$0/month. It fits in Cloudflare's free tier. The limit to watch is Workers' 100,000
requests/day: each game server sends one request every 10 seconds (8,640 a day), so about
10 servers running all day would hit it. At peak (100 players) I'm close to that, so if
traffic grows I'll change the flush interval to 30 seconds, which fits about 30 servers.
