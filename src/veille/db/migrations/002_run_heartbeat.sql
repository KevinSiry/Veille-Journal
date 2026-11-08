-- 002_run_heartbeat: run lock liveness. The lock is stale when COALESCE(heartbeat_at,
-- started_at) is older than 30 minutes (UTC ISO-8601 text, like every timestamp).

ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
