STALE_AFTER = 90
DAY = 86400


def minute_statements(now: int) -> list:
    stale = now - STALE_AFTER
    return [
        ("UPDATE servers SET closed_at = last_seen WHERE closed_at IS NULL AND last_seen < ?", [stale]),
        # Any open session whose server is closed or was never seen (died before its first
        # presence). Closed at the server's last presence, the last moment it was known online.
        (
            "UPDATE sessions SET"
            " left_at = MAX(joined_at, COALESCE((SELECT last_seen FROM servers s WHERE s.job_id = sessions.job_id), joined_at)),"
            " seconds = MAX(0, COALESCE((SELECT last_seen FROM servers s WHERE s.job_id = sessions.job_id), joined_at) - joined_at),"
            " end_reason = 'timeout'"
            " WHERE left_at IS NULL AND joined_at < ?"
            " AND NOT EXISTS (SELECT 1 FROM servers s WHERE s.job_id = sessions.job_id AND s.closed_at IS NULL)",
            [stale],
        ),
        (
            "INSERT INTO ccu_minute (minute, ccu, servers)"
            " SELECT ?, COALESCE(SUM(json_array_length(players_json)), 0), COUNT(*)"
            " FROM servers WHERE closed_at IS NULL AND private = 0 AND studio = 0"
            " ON CONFLICT(minute) DO UPDATE SET ccu = excluded.ccu, servers = excluded.servers",
            [now - now % 60],
        ),
    ]


def retention_statements(now: int) -> list:
    return [
        ("DELETE FROM sessions WHERE joined_at < ?", [now - 90 * DAY]),
        ("DELETE FROM rounds WHERE started_at < ?", [now - 90 * DAY]),
        ("DELETE FROM ccu_minute WHERE minute < ?", [now - 90 * DAY]),
        ("DELETE FROM servers WHERE closed_at < ?", [now - 7 * DAY]),
        ("DELETE FROM events WHERE at < ?", [now - 14 * DAY]),
    ]


def statements_for(now: int) -> list:
    stmts = minute_statements(now)
    if now % DAY < 60:
        stmts += retention_statements(now)
    return stmts
