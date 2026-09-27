"""Turn a Log.luau batch into D1 statements. Pure: no I/O, so tests run it against sqlite3."""

import hashlib
import json

MAX_BODY = 256 * 1024
MAX_EVENTS = 500

# Never copied into `events`: they have their own tables, and presence would dominate the budget.
OWN_TABLE = {"session_start", "session_end", "presence"}


class BadRequest(Exception):
    pass


def parse_batch(raw: bytes) -> list:
    if len(raw) > MAX_BODY:
        raise BadRequest("body over 256 KB")
    try:
        data = json.loads(raw)
    except ValueError:
        raise BadRequest("not JSON")
    if not isinstance(data, list):
        raise BadRequest("not a JSON array")
    if len(data) > MAX_EVENTS:
        raise BadRequest("over 500 events")
    return data


def _int(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _flag(v) -> int:
    return 1 if v is True or v == 1 else 0


def _first(ev: dict, *keys):
    for k in keys:
        if ev.get(k) is not None:
            return ev[k]
    return None


def session_start(ev, job, at, pv):
    return [(
        "INSERT INTO sessions (session_id, user_id, job_id, place_version, joined_at,"
        " account_age_days, followed, private, studio)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
        [ev["sessionId"], _int(ev["userId"]), job, pv, _int(ev.get("joinedAt")) or at,
         _int(ev.get("accountAgeDays")), _flag(ev.get("followed")),
         _flag(ev.get("private")), _flag(ev.get("studio"))],
    )]


def session_end(ev, job, at, pv):
    joined = _int(ev.get("joinedAt")) or at
    left = _int(ev.get("leftAt")) or at
    seconds = _int(ev.get("seconds"))
    if seconds is None:
        seconds = max(0, left - joined)
    return [(
        "INSERT INTO sessions (session_id, user_id, job_id, place_version, joined_at, left_at,"
        " seconds, afk_seconds, practice_seconds, round_seconds, rounds, end_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(session_id) DO UPDATE SET left_at = excluded.left_at,"
        " seconds = excluded.seconds, afk_seconds = excluded.afk_seconds,"
        " practice_seconds = excluded.practice_seconds, round_seconds = excluded.round_seconds,"
        " rounds = excluded.rounds, end_reason = excluded.end_reason"
        # A replayed batch writes nothing.
        " WHERE sessions.left_at IS NOT excluded.left_at"
        " OR sessions.end_reason IS NOT excluded.end_reason"
        " OR sessions.seconds IS NOT excluded.seconds",
        [ev["sessionId"], _int(ev["userId"]), job, pv, joined, left, seconds,
         _int(ev.get("afkSeconds")), _int(ev.get("practiceSeconds")),
         _int(ev.get("roundSeconds")), _int(ev.get("rounds")), ev.get("reason") or "left"],
    )]


def presence(ev, job, at, pv, now):
    players = [p for p in ev.get("players") or [] if isinstance(p, dict) and p.get("s")]
    players_json = json.dumps(players, separators=(",", ":"))
    ids_json = json.dumps([p["s"] for p in players])
    private, studio = _flag(ev.get("private")), _flag(ev.get("studio"))
    return [
        # An open session missing from a snapshot lost its session_end. Close it at the
        # previous snapshot, the last time it was known to be online.
        (
            "UPDATE sessions SET"
            " left_at = MAX(joined_at, COALESCE((SELECT last_seen FROM servers WHERE job_id = ?1), ?2)),"
            " seconds = MAX(0, COALESCE((SELECT last_seen FROM servers WHERE job_id = ?1), ?2) - joined_at),"
            " end_reason = 'timeout'"
            " WHERE job_id = ?1 AND left_at IS NULL AND joined_at < ?2"
            " AND session_id NOT IN (SELECT value FROM json_each(?3))",
            [job, at, ids_json],
        ),
        # The cron closed this server during an outage but it is still alive: reopen.
        (
            "UPDATE sessions SET left_at = NULL, seconds = NULL, end_reason = NULL"
            " WHERE end_reason = 'timeout' AND job_id = ?1"
            " AND session_id IN (SELECT value FROM json_each(?2))",
            [job, ids_json],
        ),
        # Heals a lost session_start.
        (
            "INSERT INTO sessions (session_id, user_id, job_id, place_version, joined_at, private, studio)"
            " SELECT json_extract(value, '$.s'), json_extract(value, '$.u'), ?, ?,"
            " json_extract(value, '$.t'), ?, ? FROM json_each(?) WHERE true"
            " ON CONFLICT(session_id) DO NOTHING",
            [job, pv, private, studio, players_json],
        ),
        (
            "INSERT INTO servers (job_id, place_version, private, studio, max_players, first_seen,"
            " last_seen, received_at, players_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job_id) DO UPDATE SET place_version = excluded.place_version,"
            " private = excluded.private, studio = excluded.studio,"
            " max_players = excluded.max_players, last_seen = excluded.last_seen,"
            " received_at = excluded.received_at, players_json = excluded.players_json,"
            " closed_at = NULL"
            " WHERE excluded.last_seen > servers.last_seen",
            [job, pv, private, studio, _int(ev.get("maxPlayers")), at, at, now, players_json],
        ),
    ]


def server_close(ev, job, at, pv, now):
    return [
        (
            "UPDATE sessions SET left_at = MAX(joined_at, ?1), seconds = MAX(0, ?1 - joined_at),"
            " end_reason = 'shutdown' WHERE job_id = ?2 AND left_at IS NULL",
            [at, job],
        ),
        (
            "INSERT INTO servers (job_id, place_version, first_seen, last_seen, received_at,"
            " players_json, closed_at) VALUES (?, ?, ?, ?, ?, '[]', ?)"
            " ON CONFLICT(job_id) DO UPDATE SET closed_at = excluded.closed_at, players_json = '[]'"
            " WHERE servers.closed_at IS NOT excluded.closed_at",
            [job, pv, at, at, now, at],
        ),
    ]


# Field names for round events are guessed from common spellings; the raw events are
# also kept in `events`, so a wrong guess can be backfilled.
def round_start(ev, job, at, pv, round_id):
    return [(
        "INSERT INTO rounds (round_id, job_id, place_version, private, studio, started_at, map,"
        " players, hiders) VALUES (?1, ?2, ?3,"
        " COALESCE((SELECT private FROM servers WHERE job_id = ?2), 0),"
        " COALESCE((SELECT studio FROM servers WHERE job_id = ?2), 0), ?4, ?5, ?6, ?7)"
        " ON CONFLICT(round_id) DO UPDATE SET started_at = excluded.started_at,"
        " map = excluded.map, players = excluded.players, hiders = excluded.hiders"
        " WHERE rounds.started_at IS NOT excluded.started_at OR rounds.map IS NOT excluded.map",
        [round_id, job, pv, at, _first(ev, "map", "mapName"),
         _int(_first(ev, "players", "playerCount")), _int(_first(ev, "hiders", "hiderCount"))],
    )]


def round_end(ev, job, at, pv, round_id):
    duration = _int(_first(ev, "duration", "seconds"))
    started = at - duration if duration else at
    return [(
        "INSERT INTO rounds (round_id, job_id, place_version, private, studio, started_at,"
        " ended_at, end_reason, winning_role) VALUES (?1, ?2, ?3,"
        " COALESCE((SELECT private FROM servers WHERE job_id = ?2), 0),"
        " COALESCE((SELECT studio FROM servers WHERE job_id = ?2), 0), ?4, ?5, ?6, ?7)"
        " ON CONFLICT(round_id) DO UPDATE SET ended_at = excluded.ended_at,"
        " end_reason = excluded.end_reason, winning_role = excluded.winning_role"
        " WHERE rounds.ended_at IS NOT excluded.ended_at",
        [round_id, job, pv, started, at, _first(ev, "reason", "endReason"),
         _first(ev, "winningRole", "winner", "winners")],
    )]


def store_event(ev, job, at, now, round_id):
    body = json.dumps(ev, separators=(",", ":"), sort_keys=True)
    key = hashlib.sha1(body.encode()).hexdigest()
    return [(
        "INSERT INTO events (key, at, received_at, job_id, round_id, event, level, body)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO NOTHING",
        [key, at, now, job, round_id, ev["event"], ev.get("level") or "INFO", body],
    )]


def statements_for(events: list, now: int, drop_info: set) -> tuple[list, int]:
    """Returns (statements, skipped). Malformed events are skipped, not fatal: rejecting
    the batch would make Log drop every good event in it too."""
    out, skipped = [], 0
    for ev in events:
        try:
            name, at = ev["event"], _int(ev["at"])
            if not isinstance(name, str) or at is None:
                raise ValueError
            job = ev.get("jobId") or ""
            pv = _int(ev.get("placeVersion"))
            # GameLoop's roundId is a counter per server (1, 2, 3...), so it is only unique with the job.
            round_id = f"{job}:{ev['roundId']}" if ev.get("roundId") is not None else None
            if name == "session_start":
                out += session_start(ev, job, at, pv)
            elif name == "session_end":
                out += session_end(ev, job, at, pv)
            elif name == "presence":
                out += presence(ev, job, at, pv, now)
            elif name == "server_close":
                out += server_close(ev, job, at, pv, now)
            elif name == "round_start" and round_id:
                out += round_start(ev, job, at, pv, round_id)
            elif name == "round_end" and round_id:
                out += round_end(ev, job, at, pv, round_id)

            level = ev.get("level") or "INFO"
            if name in OWN_TABLE or (level == "INFO" and name in drop_info):
                continue
            out += store_event(ev, job, at, now, round_id)
        except (KeyError, TypeError, ValueError, AttributeError):
            skipped += 1
    return out, skipped
