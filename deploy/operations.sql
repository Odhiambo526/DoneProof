-- Operator-only queries. Run in an isolated monitoring role; never expose rows
-- containing evidence or credentials to a public diagnostics endpoint.
SELECT version FROM schema_migrations ORDER BY version;
SELECT state, count(*) AS jobs,
       max(extract(epoch FROM clock_timestamp())-created_at) FILTER (WHERE finished_at IS NULL) AS oldest_open_seconds
FROM verification_jobs GROUP BY state;
SELECT percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP (ORDER BY finished_at-created_at) AS latency_seconds,
       percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP (ORDER BY started_at-created_at) AS queue_wait_seconds
FROM verification_jobs WHERE finished_at > extract(epoch FROM clock_timestamp())-3600;
SELECT count(*) AS stale_leases FROM verification_jobs
WHERE finished_at IS NULL AND lease_token IS NOT NULL AND lease_until < extract(epoch FROM clock_timestamp());
SELECT outcome,error_code,count(*) FROM verification_attempts
WHERE started_at > extract(epoch FROM clock_timestamp())-3600 GROUP BY outcome,error_code;
SELECT provider,percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP
       (ORDER BY (observation_json::jsonb->>'latency_ms')::double precision) AS observation_ms
FROM verification_conditions WHERE observation_json IS NOT NULL GROUP BY provider;
SELECT verdict,count(*) FROM receipts GROUP BY verdict;
SELECT provider,state,count(*) FROM connections GROUP BY provider,state;
SELECT state,error_code,count(*) FROM verification_callback_outbox GROUP BY state,error_code;
SELECT count(*) AS screenshots,sum(size_bytes) AS plaintext_bytes,
       count(*) FILTER (WHERE expires_at < extract(epoch FROM clock_timestamp())) AS expired
FROM browser_artifacts;
SELECT relname,n_live_tup,pg_total_relation_size(relid) AS bytes FROM pg_stat_user_tables ORDER BY bytes DESC;
SELECT state,count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY state;
-- Correlation lookup: supply an operator-selected trace ID as a bound parameter.
-- audit_events.metadata_json trace_id -> assurance_sessions.id ->
-- assurance_requests.job_id -> verification_jobs.receipt_id ->
-- verification_callback_outbox.event_id -> rc_callback_inbox.event_id.
-- Never place trace IDs, tenant IDs, message text, URLs or selectors in metric labels.
