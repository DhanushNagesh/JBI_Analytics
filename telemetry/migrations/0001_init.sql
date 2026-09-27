CREATE TABLE servers (
  job_id        TEXT PRIMARY KEY,
  place_version INTEGER,
  private       INTEGER NOT NULL DEFAULT 0,
  studio        INTEGER NOT NULL DEFAULT 0,
  max_players   INTEGER,
  first_seen    INTEGER NOT NULL,
  last_seen     INTEGER NOT NULL,   -- `at` of the latest presence
  received_at   INTEGER NOT NULL,   -- Worker clock, for spotting skew
  players_json  TEXT NOT NULL,      -- the latest presence.players, verbatim
  closed_at     INTEGER             -- set by server_close or the cron
);

CREATE TABLE sessions (
  session_id       TEXT PRIMARY KEY,
  user_id          INTEGER NOT NULL,
  job_id           TEXT NOT NULL,
  place_version    INTEGER,
  joined_at        INTEGER NOT NULL,
  left_at          INTEGER,          -- NULL while open
  seconds          INTEGER,
  afk_seconds      INTEGER,
  practice_seconds INTEGER,
  round_seconds    INTEGER,
  rounds           INTEGER,
  end_reason       TEXT,             -- left | shutdown | timeout
  account_age_days INTEGER,
  followed         INTEGER,
  private          INTEGER NOT NULL DEFAULT 0,
  studio           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX sessions_joined ON sessions(joined_at);
CREATE INDEX sessions_open   ON sessions(job_id) WHERE left_at IS NULL;
CREATE INDEX sessions_user   ON sessions(user_id, joined_at);

CREATE TABLE ccu_minute (
  minute  INTEGER PRIMARY KEY,       -- unix seconds, floored to 60
  ccu     INTEGER NOT NULL,          -- public, non-Studio servers only
  servers INTEGER NOT NULL
);

CREATE TABLE rounds (
  round_id      TEXT PRIMARY KEY,
  job_id        TEXT NOT NULL,
  place_version INTEGER,
  private       INTEGER NOT NULL DEFAULT 0,
  studio        INTEGER NOT NULL DEFAULT 0,
  started_at    INTEGER NOT NULL,
  ended_at      INTEGER,
  map           TEXT,
  players       INTEGER,
  hiders        INTEGER,
  end_reason    TEXT,
  winning_role  TEXT
);
CREATE INDEX rounds_started ON rounds(started_at);

-- Everything else Log sends. `key` is a hash of the raw event so a re-sent batch is a no-op.
CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  key         TEXT NOT NULL UNIQUE,
  at          INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  job_id      TEXT,
  round_id    TEXT,
  event       TEXT NOT NULL,
  level       TEXT NOT NULL,
  body        TEXT NOT NULL
);
CREATE INDEX events_at ON events(at);
