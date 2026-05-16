-- Inventory dataset for the multi-source demo. Mounted by quack-server-2
-- over the baked-in /quack/seed.sql, so the same image serves two
-- conceptually disjoint datasets depending on which volume is attached.

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR  NOT NULL,
    category    VARCHAR  NOT NULL,
    stock_qty   INTEGER  NOT NULL,
    reorder_at  INTEGER  NOT NULL,
    unit_price  DECIMAL(18, 2) NOT NULL
);

INSERT INTO products VALUES
    ( 1, 'Widget',             'Hardware',    120,  25,  12.50),
    ( 2, 'Sprocket',           'Hardware',     18,  30,   8.75),
    ( 3, 'Gizmo',              'Hardware',     72,  20,  22.00),
    ( 4, 'Bracket M4',         'Fasteners',   450, 100,   0.95),
    ( 5, 'Bolt M6 x 20',       'Fasteners',   220, 200,   0.55),
    ( 6, 'Hex Nut M6',         'Fasteners',    85, 150,   0.20),
    ( 7, 'O-Ring 12mm',        'Seals',        60,  40,   1.10),
    ( 8, 'Gasket Sheet A4',    'Seals',        12,  20,  14.00),
    ( 9, 'Bearing 608ZZ',      'Bearings',    140,  50,   3.25),
    (10, 'Bearing 6203',       'Bearings',     28,  40,   5.40),
    (11, 'Cable Loom 2m',      'Cabling',      95,  60,   6.80),
    (12, 'Heatshrink Pack',    'Cabling',       8,  25,   2.40),
    (13, 'Soldering Iron',     'Tools',        14,  10,  45.00),
    (14, 'Multimeter',         'Tools',         6,   5,  78.50),
    (15, 'Caliper 150mm',      'Tools',        11,   8,  24.50),
    (16, 'Sandpaper Roll',     'Consumables', 320,  80,   3.10),
    (17, 'Cutting Disc 125',   'Consumables',  22,  30,   2.75),
    (18, 'Threadlock 50ml',    'Consumables',  44,  25,   9.20),
    (19, 'Cleaning Solvent',   'Consumables', 130,  50,   7.65),
    (20, 'Calibration Gauge',  'Tools',         2,   3, 220.00);
