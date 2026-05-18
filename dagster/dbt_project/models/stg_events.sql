-- Staging model — light type/column cleanup over the dlt-loaded raw
-- table. Centralizes any rename / cast logic so downstream marts
-- don't have to know about dlt's load-time column names.
--
-- Materialized as a view since downstream models reference it
-- in-process; no need to land another Parquet snapshot.

{{ config(materialized='view') }}

SELECT
    event_id,
    ts                       AS event_ts,
    CAST(ts AS DATE)         AS event_date,
    user_id,
    country,
    page,
    event_type,
    session_id
FROM {{ source('raw', 'events') }}
