-- Daily traffic mart — one row per (date, country). Page views and
-- distinct users per slice. Materialized as Parquet under
-- /data/marts/daily_traffic.parquet by dbt-duckdb's `external`
-- materialization; quack-server-3 creates a VIEW over it so the
-- Toolbox-side ATTACH sees it as analytics_engineered_remote.daily_traffic.

SELECT
    event_date,
    country,
    COUNT(*) FILTER (WHERE event_type = 'page_view')   AS page_views,
    COUNT(DISTINCT user_id)                            AS unique_users,
    COUNT(DISTINCT session_id)                         AS sessions
FROM {{ ref('stg_events') }}
GROUP BY event_date, country
ORDER BY event_date DESC, page_views DESC
