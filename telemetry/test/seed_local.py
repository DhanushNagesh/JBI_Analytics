"""Writes a day and a half of fake history to stdout as SQL, for eyeballing the dashboard locally:
python3 test/seed_local.py > /tmp/seed.sql && npx wrangler d1 execute DB --local --file /tmp/seed.sql"""
import math, random, time
random.seed(7)
now = int(time.time()); now -= now % 60
out = []
for m in range(now - 36 * 3600, now, 60):
    h = (m % 86400) / 3600
    ccu = max(0, round(14 + 10 * math.sin((h - 14) / 24 * 2 * math.pi) + random.gauss(0, 1.5)))
    out.append(f"INSERT OR REPLACE INTO ccu_minute VALUES ({m},{ccu},{max(1, math.ceil(ccu / 9))});")
for i in range(260):
    j = now - random.randint(600, 30 * 3600)
    secs = int(random.lognormvariate(6.5, 1.1))
    left = min(j + secs, now - 60)
    r = max(0, (left - j) // 240)
    out.append(f"INSERT OR IGNORE INTO sessions (session_id,user_id,job_id,place_version,joined_at,left_at,seconds,"
               f"afk_seconds,practice_seconds,round_seconds,rounds,end_reason) VALUES ('seed{i}',{random.randint(1000,1080)},"
               f"'seedjob',42,{j},{left},{left - j},{(left - j) // 12},{(left - j) // 20},{(left - j) // 2},{r},'left');")
for i in range(140):
    s = now - random.randint(300, 30 * 3600)
    reason, win = random.choice([("all_tagged", "seeker"), ("timer", "hider"), ("timer", "hider"), ("all_tagged", "seeker"), ("not_enough_players", None)])
    out.append(f"INSERT OR IGNORE INTO rounds (round_id,job_id,place_version,started_at,ended_at,end_reason,winning_role) VALUES "
               f"('seedr{i}','seedjob',42,{s},{s + 200},'{reason}',{f'{chr(39)}{win}{chr(39)}' if win else 'NULL'});")
print("\n".join(out))
