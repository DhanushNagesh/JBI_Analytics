-- D1 and D7 retention by first-seen day. Not on the dashboard yet; run it by hand:
--   npx wrangler d1 execute DB --remote --file queries/retention.sql
-- "First day" is the first day this pipeline saw the player, not their first visit ever.
-- Cohorts younger than 7 days show 0 for D7 because day 7 hasn't happened yet.
WITH days AS (
  SELECT DISTINCT user_id, joined_at / 86400 AS day
  FROM sessions
  WHERE private = 0 AND studio = 0
),
cohort AS (
  SELECT user_id, MIN(day) AS first_day FROM days GROUP BY user_id
)
SELECT
  date(c.first_day * 86400, 'unixepoch') AS cohort_day,
  COUNT(*) AS new_players,
  ROUND(100.0 * SUM(EXISTS (SELECT 1 FROM days d WHERE d.user_id = c.user_id AND d.day = c.first_day + 1)) / COUNT(*), 1) AS d1_pct,
  ROUND(100.0 * SUM(EXISTS (SELECT 1 FROM days d WHERE d.user_id = c.user_id AND d.day = c.first_day + 7)) / COUNT(*), 1) AS d7_pct
FROM cohort c
GROUP BY c.first_day
ORDER BY c.first_day;
