-- Sample analytics dataset used by the MCP Toolbox DuckDB/Quack demo.
-- Two related tables, ~30 rows total. Enough to exercise GROUP BY, JOIN,
-- ILIKE patterns, and DATE filters in the curated tools.

CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY,
    customer    VARCHAR  NOT NULL,
    amount      DECIMAL(18, 2) NOT NULL,
    order_date  DATE     NOT NULL
);

INSERT INTO sales VALUES
    ( 1, 'Alice GmbH',    1200.50, DATE '2026-01-04'),
    ( 2, 'Alice GmbH',     330.00, DATE '2026-01-19'),
    ( 3, 'Bob Corp',       875.20, DATE '2026-01-28'),
    ( 4, 'Carol AG',       540.00, DATE '2026-02-02'),
    ( 5, 'Daniel SARL',   2100.00, DATE '2026-02-11'),
    ( 6, 'Alice GmbH',     150.75, DATE '2026-02-22'),
    ( 7, 'Bob Corp',      1480.00, DATE '2026-03-01'),
    ( 8, 'Eva Ltd',         95.00, DATE '2026-03-08'),
    ( 9, 'Carol AG',       710.00, DATE '2026-03-19'),
    (10, 'Frank GmbH',     410.00, DATE '2026-03-25'),
    (11, 'Alice GmbH',     980.40, DATE '2026-04-02'),
    (12, 'Bob Corp',       265.00, DATE '2026-04-10'),
    (13, 'Daniel SARL',   1750.00, DATE '2026-04-17'),
    (14, 'Eva Ltd',        120.00, DATE '2026-04-25'),
    (15, 'Carol AG',       640.50, DATE '2026-05-03');

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    customer    VARCHAR  NOT NULL,
    product     VARCHAR  NOT NULL,
    qty         INTEGER  NOT NULL,
    order_date  DATE     NOT NULL
);

INSERT INTO orders VALUES
    ( 1, 'Alice GmbH', 'Widget',   12, DATE '2026-01-04'),
    ( 2, 'Alice GmbH', 'Sprocket',  3, DATE '2026-01-19'),
    ( 3, 'Bob Corp',   'Widget',    8, DATE '2026-01-28'),
    ( 4, 'Carol AG',   'Gizmo',     5, DATE '2026-02-02'),
    ( 5, 'Daniel SARL','Widget',   20, DATE '2026-02-11'),
    ( 6, 'Alice GmbH', 'Gizmo',     2, DATE '2026-02-22'),
    ( 7, 'Bob Corp',   'Sprocket', 14, DATE '2026-03-01'),
    ( 8, 'Eva Ltd',    'Widget',    1, DATE '2026-03-08'),
    ( 9, 'Carol AG',   'Widget',    7, DATE '2026-03-19'),
    (10, 'Frank GmbH', 'Gizmo',     4, DATE '2026-03-25'),
    (11, 'Alice GmbH', 'Sprocket',  9, DATE '2026-04-02'),
    (12, 'Bob Corp',   'Gizmo',     3, DATE '2026-04-10'),
    (13, 'Daniel SARL','Sprocket', 17, DATE '2026-04-17'),
    (14, 'Eva Ltd',    'Gizmo',     2, DATE '2026-04-25'),
    (15, 'Carol AG',   'Sprocket',  6, DATE '2026-05-03');
