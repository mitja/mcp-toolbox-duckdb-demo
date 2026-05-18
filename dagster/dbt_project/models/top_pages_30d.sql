-- Top-pages mart — one row per page, restricted to the trailing
-- 30 days of activity. Materialized as Parquet under
-- /data/marts/top_pages_30d.parquet.

WITH window_events AS (
    SELECT *
    FROM {{ ref('stg_events') }}
    WHERE event_ts >= (SELECT MAX(event_ts) FROM {{ ref('stg_events') }}) - INTERVAL '30 DAY'
)

SELECT
    page,
    COUNT(*) FILTER (WHERE event_type = 'page_view')   AS page_views_30d,
    COUNT(DISTINCT user_id)                            AS unique_users_30d,
    COUNT(DISTINCT session_id)                         AS sessions_30d
FROM window_events
GROUP BY page
ORDER BY page_views_30d DESC
