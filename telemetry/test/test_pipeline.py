"""Runs the Worker's SQL against sqlite3 with the real migration. D1 is SQLite, so the
statements behave the same; only the binding layer in entry.py is not covered here."""

import asyncio
import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import api  # noqa: E402
import cron  # noqa: E402
import ingest  # noqa: E402

T0 = 1_790_000_000
JOB = "job-a"


def d1_params(params):
    # D1 receives numbers from JS, so integers arrive bound as REAL. Mirror that here or
    # integer-division bugs pass locally and fail on D1.
    return [float(p) if isinstance(p, int) and not isinstance(p, bool) else p for p in params]


class DB:
    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        for m in sorted((ROOT / "migrations").glob("*.sql")):
            self.c.executescript(m.read_text())

    def run(self, stmts):
        with self.c:
            for sql, params in stmts:
                self.c.execute(sql, d1_params(params))

    async def all(self, sql, params=()):
        return [dict(r) for r in self.c.execute(sql, d1_params(params))]

    def rows(self, sql, params=()):
        return [dict(r) for r in self.c.execute(sql, params)]

    def counts(self):
        return {t: self.c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("servers", "sessions", "ccu_minute", "rounds", "events")}


def env(event, at, **kw):
    return {"event": event, "level": "INFO", "jobId": JOB, "placeVersion": 42, "at": at,
            "uptime": at - T0, **kw}


def start(uid, sid, at):
    return env("session_start", at, userId=uid, sessionId=sid, joinedAt=at,
               accountAgeDays=400, followed=False, private=False, studio=False)


def end(uid, sid, joined, left, reason="left"):
    return env("session_end", left, userId=uid, sessionId=sid, joinedAt=joined, leftAt=left,
               seconds=left - joined, afkSeconds=10, practiceSeconds=5, roundSeconds=100,
               rounds=2, reason=reason)


def pres(at, players):
    return env("presence", at, maxPlayers=11, private=False, studio=False,
               players=[{"u": u, "s": s, "t": t, "st": st} for u, s, t, st in players])


def ingest_batch(db, events, now):
    stmts, skipped = ingest.statements_for(events, now, {"shot"})
    db.run(stmts)
    return skipped


def q(**kw):
    return {k: str(v) for k, v in kw.items()}


def test_parse_batch_rejects_bad_bodies():
    with pytest.raises(ingest.BadRequest):
        ingest.parse_batch(b'{"event":"x"}')
    with pytest.raises(ingest.BadRequest):
        ingest.parse_batch(b"not json")
    with pytest.raises(ingest.BadRequest):
        ingest.parse_batch(b"[" + b",".join([b"{}"] * 501) + b"]")
    with pytest.raises(ingest.BadRequest):
        ingest.parse_batch(b" " * (256 * 1024 + 1))
    assert ingest.parse_batch(b"[]") == []


def test_replayed_batch_changes_nothing():
    db = DB()
    batch = [start(1, "s1", T0), pres(T0 + 30, [(1, "s1", T0, "lobby")]),
             env("round_start", T0 + 40, roundId="r1", map="Mall", players=1, hiders=0),
             env("hider_tagged", T0 + 50, level="WARN", roundId="r1", userId=1),
             env("shot", T0 + 51), end(1, "s1", T0, T0 + 60)]
    ingest_batch(db, batch, T0 + 61)
    first = (db.counts(), db.rows("SELECT * FROM sessions"), db.rows("SELECT * FROM servers"))
    ingest_batch(db, batch, T0 + 62)
    assert (db.counts(), db.rows("SELECT * FROM sessions")) == first[:2]
    assert db.rows("SELECT received_at FROM servers")[0]["received_at"] == T0 + 61
    assert db.counts()["events"] == 2  # round_start + hider_tagged; shot dropped


def test_malformed_events_are_skipped_not_fatal():
    db = DB()
    skipped = ingest_batch(db, [{"nope": 1}, "str", env("session_start", T0), start(1, "s1", T0)], T0)
    assert skipped == 3
    assert db.counts()["sessions"] == 1


def test_presence_heals_lost_start_and_end():
    db = DB()
    ingest_batch(db, [pres(T0 + 30, [(1, "s1", T0, "lobby"), (2, "s2", T0 + 5, "hider")])], T0 + 31)
    assert db.counts()["sessions"] == 2
    # s2's session_end is lost; the next snapshot no longer lists it.
    ingest_batch(db, [pres(T0 + 60, [(1, "s1", T0, "seeker")])], T0 + 61)
    s2 = db.rows("SELECT * FROM sessions WHERE session_id = 's2'")[0]
    assert (s2["left_at"], s2["seconds"], s2["end_reason"]) == (T0 + 30, 25, "timeout")
    assert db.rows("SELECT left_at FROM sessions WHERE session_id = 's1'")[0]["left_at"] is None


def test_session_end_alone_creates_complete_row():
    db = DB()
    ingest_batch(db, [end(7, "s7", T0, T0 + 1800)], T0 + 1800)
    r = db.rows("SELECT * FROM sessions")[0]
    assert (r["user_id"], r["seconds"], r["rounds"], r["end_reason"]) == (7, 1800, 2, "left")


def test_server_close_closes_open_sessions():
    db = DB()
    ingest_batch(db, [start(1, "s1", T0), start(2, "s2", T0 + 10), end(2, "s2", T0 + 10, T0 + 20),
                      pres(T0 + 30, [(1, "s1", T0, "lobby")]), env("server_close", T0 + 45)], T0 + 46)
    rows = {r["session_id"]: r for r in db.rows("SELECT * FROM sessions")}
    assert (rows["s1"]["end_reason"], rows["s1"]["seconds"]) == ("shutdown", 45)
    assert rows["s2"]["end_reason"] == "left"
    assert db.rows("SELECT closed_at FROM servers")[0]["closed_at"] == T0 + 45


def test_cron_times_out_silent_server_and_presence_reopens_it():
    db = DB()
    ingest_batch(db, [start(1, "s1", T0), pres(T0 + 30, [(1, "s1", T0, "lobby")])], T0 + 31)
    db.run(cron.statements_for(T0 + 100))  # 70 s of silence: not stale yet
    assert db.rows("SELECT left_at FROM sessions")[0]["left_at"] is None
    db.run(cron.statements_for(T0 + 121))
    s = db.rows("SELECT * FROM sessions")[0]
    assert (s["left_at"], s["seconds"], s["end_reason"]) == (T0 + 30, 30, "timeout")
    assert db.rows("SELECT closed_at FROM servers")[0]["closed_at"] == T0 + 30
    # Ingest was only down, the server is alive: the next presence reopens both.
    ingest_batch(db, [pres(T0 + 210, [(1, "s1", T0, "hider")])], T0 + 211)
    s = db.rows("SELECT * FROM sessions")[0]
    assert (s["left_at"], s["end_reason"]) == (None, None)
    assert db.rows("SELECT closed_at FROM servers")[0]["closed_at"] is None


def test_cron_closes_session_whose_server_never_sent_presence():
    db = DB()
    ingest_batch(db, [start(1, "s1", T0)], T0)
    db.run(cron.statements_for(T0 + 200))
    s = db.rows("SELECT * FROM sessions")[0]
    assert (s["seconds"], s["end_reason"]) == (0, "timeout")


def test_ccu_minute_counts_public_non_studio_only():
    db = DB()
    now = T0 - T0 % 60 + 30
    ingest_batch(db, [pres(now, [(1, "a", now, "lobby"), (2, "b", now, "afk")]),
                      {**pres(now, [(3, "c", now, "lobby")]), "jobId": "priv", "private": True},
                      {**pres(now, [(4, "d", now, "lobby")]), "jobId": "", "studio": True}], now)
    db.run(cron.statements_for(now + 5))
    assert db.rows("SELECT ccu, servers FROM ccu_minute") == [{"ccu": 2, "servers": 1}]
    db.run(cron.statements_for(now + 6))  # same minute again: replaced, not duplicated
    assert db.counts()["ccu_minute"] == 1


def test_retention_runs_at_utc_midnight_only():
    assert len(cron.statements_for(T0 - T0 % 86400 + 30)) == 8
    assert len(cron.statements_for(T0 - T0 % 86400 + 90)) == 3


def test_live_and_summary():
    db = DB()
    day = T0 - T0 % 86400
    ingest_batch(db, [
        start(1, "s1", day - 600), end(1, "s1", day - 600, day + 600),       # crosses midnight
        start(2, "s2", day + 1000), end(2, "s2", day + 1000, day + 1030),    # 30 s
        start(2, "s3", day + 2000),                                          # still open
        pres(day + 2100, [(2, "s3", day + 2000, "hider")]),
        {**start(9, "p1", day + 100), "jobId": "priv", "private": True},
    ], day + 2100)
    now = day + 2400
    live = asyncio.run(api.live(db, {}, now))
    assert (live["ccu"], live["states"]["hider"], live["stale"]) == (1, 1, True)

    s = asyncio.run(api.summary(db, q(**{"from": day, "to": now}), now))
    # s1: 600 s after midnight, s2: 30 s, s3: open for 400 s
    assert s["hours"] == round(1030 / 3600, 2)
    assert (s["uniquePlayers"], s["sessions"], s["openSessions"], s["closedSessions"]) == (2, 3, 1, 1)
    assert s["medianSeconds"] == 30
    assert {h["bucket"]: h["n"] for h in s["histogram"]}["<1"] == 1

    s_priv = asyncio.run(api.summary(db, q(**{"from": day, "to": now, "private": 1}), now))
    assert s_priv["uniquePlayers"] == 3

    top = asyncio.run(api.top(db, q(**{"from": day, "to": now}), now))
    assert [(p["userId"], p["seconds"]) for p in top["players"]] == [(1, 600), (2, 430)]


def test_badges():
    db = DB()
    day = T0 - T0 % 86400
    empty = asyncio.run(api.badge(db, "playtime", day + 60))
    assert empty["message"] == "no sessions yet"
    ingest_batch(db, [
        start(1, "s1", day + 100), end(1, "s1", day + 100, day + 700),     # 10 min
        start(2, "s2", day + 200), end(2, "s2", day + 200, day + 290),     # 90 s
        start(3, "s3", day + 300),
        pres(day + 400, [(3, "s3", day + 300, "seeker")]),
    ], day + 400)
    assert asyncio.run(api.badge(db, "ccu", day + 450))["message"] == "1"
    assert asyncio.run(api.badge(db, "playtime", day + 800))["message"] == "5m 45s"
    with pytest.raises(KeyError):
        asyncio.run(api.badge(db, "revenue", day))


def test_ccu_series_and_previous_day():
    db = DB()
    m = T0 - T0 % 3600
    db.run([("INSERT INTO ccu_minute VALUES (?, ?, 1)", [m + i * 60, i]) for i in range(10)] +
           [("INSERT INTO ccu_minute VALUES (?, 7, 1)", [m - 86400])])
    r = asyncio.run(api.ccu(db, q(**{"from": m, "to": m + 600, "step": 300}), m + 600))
    assert [x["ccu"] for x in r["series"]] == [4, 9]
    assert r["previous"] == [{"t": m, "ccu": 7, "servers": 1}]


def test_rounds_and_delete_user():
    db = DB()
    ingest_batch(db, [pres(T0, [(1, "s1", T0, "lobby")]),
                      env("round_start", T0 + 10, roundId="r1", map="Mall"),
                      env("round_end", T0 + 200, roundId="r1", reason="all_tagged", winningRole="seeker"),
                      env("round_end", T0 + 400, roundId="r2", reason="timer", winningRole="hider", duration=180),
                      env("error_thing", T0 + 5, level="ERROR", userId=1), start(1, "s1", T0)], T0 + 400)
    r = asyncio.run(api.rounds(db, q(**{"from": T0 - 3600, "to": T0 + 3600}), T0 + 400))
    assert sum(x["n"] for x in r["perHour"]) == 2
    assert {x["role"] for x in r["winningRoles"]} == {"seeker", "hider"}
    assert db.rows("SELECT started_at FROM rounds WHERE round_id = 'job-a:r2'")[0]["started_at"] == T0 + 220

    db.run(api.delete_user_statements(1))
    assert db.counts()["sessions"] == 0
    assert all(json.loads(e["body"]).get("userId") != 1 for e in db.rows("SELECT body FROM events"))


def test_round_ids_are_per_server():
    db = DB()
    ingest_batch(db, [env("round_start", T0, roundId=1, map="Mall"),
                      {**env("round_start", T0 + 5, roundId=1, map="Pagoda"), "jobId": "job-b"},
                      env("round_end", T0 + 100, roundId=1, reason="pot filled", winningRole="Hider", seconds=99.6)], T0 + 100)
    rows = {r["round_id"]: r for r in db.rows("SELECT * FROM rounds")}
    assert set(rows) == {"job-a:1", "job-b:1"}
    assert (rows["job-a:1"]["map"], rows["job-a:1"]["winning_role"], rows["job-a:1"]["started_at"]) == ("Mall", "Hider", T0)
    assert rows["job-b:1"]["ended_at"] is None
