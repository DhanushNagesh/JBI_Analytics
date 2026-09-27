"""Read API. Each handler takes a `db` with `async all(sql, params) -> list[dict]`."""

import json

STALE_AFTER = 120
# Sessions are assumed shorter than this, so range queries can use the joined_at index.
MAX_SESSION = 86400
BUCKETS = [(60, "<1"), (300, "1-5"), (900, "5-15"), (1800, "15-30"), (3600, "30-60"), (None, "60+")]


class Filters:
    def __init__(self, q: dict):
        self.private = q.get("private") == "1"
        self.studio = q.get("studio") == "1"
        v = q.get("version") or ""
        self.version = int(v) if v.isdigit() else None

    def clause(self, prefix=""):
        parts, params = [], []
        if not self.private:
            parts.append(f"{prefix}private = 0")
        if not self.studio:
            parts.append(f"{prefix}studio = 0")
        if self.version is not None:
            parts.append(f"{prefix}place_version = ?")
            params.append(self.version)
        return "".join(" AND " + p for p in parts), params


def time_range(q: dict, now: int):
    to = int(q["to"]) if q.get("to", "").isdigit() else now
    frm = int(q["from"]) if q.get("from", "").isdigit() else to - 86400
    if frm >= to:
        raise ValueError("from must be before to")
    return frm, min(to, now)


async def live(db, q, now):
    f = Filters(q)
    where, params = f.clause()
    rows = await db.all(
        "SELECT job_id, place_version, private, studio, max_players, first_seen, last_seen, players_json"
        " FROM servers WHERE closed_at IS NULL" + where + " ORDER BY first_seen",
        params,
    )
    states = {"lobby": 0, "hider": 0, "seeker": 0, "jailed": 0, "practice": 0, "afk": 0}
    servers, players = [], []
    for r in rows:
        ps = json.loads(r["players_json"] or "[]")
        for p in ps:
            st = p.get("st") or "lobby"
            states[st] = states.get(st, 0) + 1
            players.append({"userId": p.get("u"), "state": st, "since": p.get("t"), "jobId": r["job_id"]})
        servers.append({
            "jobId": r["job_id"], "players": len(ps), "maxPlayers": r["max_players"],
            "placeVersion": r["place_version"], "private": bool(r["private"]),
            "studio": bool(r["studio"]), "firstSeen": r["first_seen"], "lastSeen": r["last_seen"],
        })
    last = max((s["lastSeen"] for s in servers), default=None)
    return {
        "now": now, "ccu": len(players), "servers": servers, "players": players, "states": states,
        "lastData": last, "stale": last is not None and now - last > STALE_AFTER,
    }


async def summary(db, q, now):
    frm, to = time_range(q, now)
    where, fp = Filters(q).clause()
    overlap = "MAX(0, MIN(COALESCE(left_at, ?), ?) - MAX(joined_at, ?))"
    in_range = " WHERE joined_at < ? AND joined_at >= ? AND (left_at IS NULL OR left_at > ?)"
    totals = (await db.all(
        f"SELECT COALESCE(SUM({overlap}), 0) AS secs, COUNT(DISTINCT user_id) AS users,"
        " COUNT(*) AS sessions, SUM(left_at IS NULL) AS open FROM sessions" + in_range + where,
        [now, to, frm, to, frm - MAX_SESSION, frm] + fp,
    ))[0]

    # Length stats cover sessions that started in the range and have ended.
    closed = " WHERE joined_at >= ? AND joined_at < ? AND left_at IS NOT NULL" + where
    cp = [frm, to] + fp
    case = "CASE " + " ".join(
        f"WHEN seconds < {lim} THEN '{label}'" for lim, label in BUCKETS if lim
    ) + f" ELSE '{BUCKETS[-1][1]}' END"
    hist_rows = await db.all(f"SELECT {case} AS bucket, COUNT(*) AS n FROM sessions" + closed + " GROUP BY bucket", cp)
    stats = (await db.all(
        "SELECT COUNT(*) AS n, AVG(seconds) AS avg, SUM(afk_seconds) AS afk, SUM(seconds) AS total"
        " FROM sessions" + closed, cp,
    ))[0]
    median = None
    n = stats["n"] or 0
    if n:
        mid = await db.all(
            "SELECT seconds FROM sessions" + closed + " ORDER BY seconds LIMIT 2 OFFSET ?",
            cp + [(n - 1) // 2],
        )
        median = mid[0]["seconds"] if n % 2 else (mid[0]["seconds"] + mid[1]["seconds"]) / 2

    counts = {r["bucket"]: r["n"] for r in hist_rows}
    return {
        "from": frm, "to": to,
        "hours": round(totals["secs"] / 3600, 2),
        "uniquePlayers": totals["users"],
        "sessions": totals["sessions"],
        "openSessions": totals["open"] or 0,
        "closedSessions": n,
        "avgSeconds": round(stats["avg"]) if stats["avg"] is not None else None,
        "medianSeconds": median,
        "afkShare": (stats["afk"] or 0) / stats["total"] if stats["total"] else None,
        "histogram": [{"bucket": label, "n": counts.get(label, 0)} for _, label in BUCKETS],
    }


async def ccu(db, q, now):
    frm, to = time_range(q, now)
    step = int(q["step"]) if q.get("step", "").isdigit() else 60
    step = max(60, step - step % 60)
    sql = (
        "SELECT (minute - minute % ?) AS t, MAX(ccu) AS ccu, MAX(servers) AS servers"
        " FROM ccu_minute WHERE minute >= ? AND minute < ? GROUP BY t ORDER BY t"
    )
    series = await db.all(sql, [step, frm, to])
    # Same window a day earlier, shifted forward so it lines up on the chart.
    prev = await db.all(sql, [step, frm - 86400, to - 86400])
    for r in prev:
        r["t"] += 86400
    return {"from": frm, "to": to, "step": step, "series": series, "previous": prev}


async def top(db, q, now):
    frm, to = time_range(q, now)
    where, fp = Filters(q).clause()
    limit = min(int(q["limit"]), 200) if q.get("limit", "").isdigit() else 25
    rows = await db.all(
        "SELECT user_id AS userId, SUM(MAX(0, MIN(COALESCE(left_at, ?), ?) - MAX(joined_at, ?))) AS seconds,"
        " COUNT(*) AS sessions, COALESCE(SUM(rounds), 0) AS rounds FROM sessions"
        " WHERE joined_at < ? AND joined_at >= ? AND (left_at IS NULL OR left_at > ?)" + where +
        " GROUP BY user_id ORDER BY seconds DESC LIMIT ?",
        [now, to, frm, to, frm - MAX_SESSION, frm] + fp + [limit],
    )
    return {"from": frm, "to": to, "players": rows}


async def rounds(db, q, now):
    frm, to = time_range(q, now)
    where, fp = Filters(q).clause()
    base = " FROM rounds WHERE started_at >= ? AND started_at < ?" + where
    p = [frm, to] + fp
    per_hour = await db.all("SELECT (started_at / 3600) * 3600 AS t, COUNT(*) AS n" + base + " GROUP BY t ORDER BY t", p)
    reasons = await db.all("SELECT COALESCE(end_reason, 'unfinished') AS reason, COUNT(*) AS n" + base + " GROUP BY reason ORDER BY n DESC", p)
    winners = await db.all("SELECT winning_role AS role, COUNT(*) AS n" + base + " AND winning_role IS NOT NULL GROUP BY role ORDER BY n DESC", p)
    return {"from": frm, "to": to, "perHour": per_hour, "endReasons": reasons, "winningRoles": winners}


def delete_user_statements(user_id: int) -> list:
    return [
        ("DELETE FROM sessions WHERE user_id = ?", [user_id]),
        ("DELETE FROM events WHERE json_extract(body, '$.userId') = ?", [user_id]),
    ]
