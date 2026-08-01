-- Event-time semantics (D13) as portable SQL, for any DuckDB-dialect
-- engine reading the Iceberg events table directly (D15 addendum).
-- Expects a view or table named `events` with the D12 columns.
--
--   CREATE VIEW events AS SELECT * FROM iceberg_scan('<metadata.json>');
--   SELECT * FROM state_at(TIMESTAMPTZ '2026-01-03 00:00:00+00');
--
-- No snapshot acceleration on purpose: portability over speed. The
-- dedupe on (resource_id, sequence) is what makes at-least-once
-- duplicate rows invisible (D12); the op filter is tombstone semantics
-- (D10): a resource whose highest sequence at t is a delete is absent.

CREATE OR REPLACE MACRO state_at(t) AS TABLE
SELECT resource_id, resource_type, attrs, relationships, sequence
FROM (
    SELECT *, row_number() OVER (PARTITION BY resource_id ORDER BY sequence DESC) AS rn
    FROM (
        SELECT DISTINCT resource_id, resource_type, attrs, relationships, sequence, op
        FROM events
        WHERE event_time <= t
    )
)
WHERE rn = 1 AND op = 'upsert'
ORDER BY resource_id;
