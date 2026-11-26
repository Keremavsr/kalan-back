-- Job ownership: the process that runs a pending job and when it last showed it is alive. On start-up a service
-- fails only pending jobs whose owner is gone or silent for too long, never another live process's job.
ALTER TABLE jobs ADD COLUMN owner_pid INTEGER;
ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT;
