-- The cron sweeps run every minute and only care about open rows. Without these they scan
-- the whole table (sessions_joined matches nearly every row), which used ~90% of D1's
-- 5M rows-read/day free limit.
CREATE INDEX sessions_open_joined ON sessions(joined_at) WHERE left_at IS NULL;
CREATE INDEX servers_open ON servers(last_seen) WHERE closed_at IS NULL;
