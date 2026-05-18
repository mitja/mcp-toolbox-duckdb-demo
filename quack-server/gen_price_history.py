"""Generate product_price_history.parquet for the inventory demo.

The companion parquet file is committed alongside this script as a
fixture (small enough — ~100 rows — that the binary diff doesn't bloat
the repo). Re-run it only if you change the schema or the underlying
products list.

The fixture is consumed by quack-server-2 (the inventory Quack server):
seed-inventory.sql creates a view in `main` over read_parquet(...) so
the Toolbox-side DuckDB sees it through the existing ATTACH. See
NOTES.md "Pattern: federate non-DuckDB backends *inside* the remote
Quack server" for the broader pattern this demonstrates.

Run:

    uv run --no-project --with duckdb python3 gen_price_history.py
"""
from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import duckdb


# Match exactly the rows in seed-inventory.sql so the "current" price
# in the parquet agrees with the products table. (product_name,
# current_unit_price).
PRODUCTS: list[tuple[str, float]] = [
    ("Widget",            12.50),
    ("Sprocket",           8.75),
    ("Gizmo",             22.00),
    ("Bracket M4",         0.95),
    ("Bolt M6 x 20",       0.55),
    ("Hex Nut M6",         0.20),
    ("O-Ring 12mm",        1.10),
    ("Gasket Sheet A4",   14.00),
    ("Bearing 608ZZ",      3.25),
    ("Bearing 6203",       5.40),
    ("Cable Loom 2m",      6.80),
    ("Heatshrink Pack",    2.40),
    ("Soldering Iron",    45.00),
    ("Multimeter",        78.50),
    ("Caliper 150mm",     24.50),
    ("Sandpaper Roll",     3.10),
    ("Cutting Disc 125",   2.75),
    ("Threadlock 50ml",    9.20),
    ("Cleaning Solvent",   7.65),
    ("Calibration Gauge",220.00),
]

REASONS = [
    "launch",
    "promo",
    "cost-up",
    "cost-down",
    "supplier-change",
    "seasonal-adjust",
    "audit-correction",
    "rebrand",
]

# Inclusive lower bound for the first history row; the current
# (valid_to IS NULL) row's valid_from lands somewhere in early 2026.
EPOCH = dt.date(2024, 1, 1)
HISTORY_PER_PRODUCT = 5  # 4 historical + 1 current


def history_for(product: str, current_price: float, rng: random.Random) -> list[dict]:
    """Generate HISTORY_PER_PRODUCT rows ending with current_price (valid_to=NULL)."""
    rows: list[dict] = []
    # Pick HISTORY_PER_PRODUCT-1 monotonically-increasing change dates
    # in [EPOCH, 2026-04-01], then tile the validity windows so each
    # row's valid_to equals the next row's valid_from.
    span_days = (dt.date(2026, 4, 1) - EPOCH).days
    cuts = sorted(rng.sample(range(60, span_days), HISTORY_PER_PRODUCT - 1))
    boundaries = [EPOCH] + [EPOCH + dt.timedelta(days=c) for c in cuts]

    # Walk backwards from current_price, applying small randomized
    # adjustments so each historical row has a distinct price. Cap
    # adjustments so we don't end up with negative prices.
    prices: list[float] = [current_price]
    for _ in range(HISTORY_PER_PRODUCT - 1):
        prev = prices[-1]
        # Adjustment in [-15%, +15%] of current row's price, with a
        # 1-cent floor on the absolute step so 0.20 items still vary.
        pct = rng.uniform(-0.15, 0.15)
        step = max(abs(prev * pct), 0.01)
        if rng.random() < 0.5:
            step = -step
        new_price = max(round(prev + step, 2), 0.05)
        prices.append(new_price)
    # `prices` is current-first; reverse so the oldest price comes first.
    prices.reverse()

    for i, valid_from in enumerate(boundaries):
        is_current = (i == len(boundaries) - 1)
        valid_to = None if is_current else boundaries[i + 1]
        reason = "launch" if i == 0 else rng.choice(REASONS[1:])
        rows.append({
            "product_name": product,
            "valid_from":   valid_from,
            "valid_to":     valid_to,
            "unit_price":   prices[i],
            "change_reason": reason,
        })
    return rows


def main() -> None:
    rng = random.Random(20260518)  # fixed seed → reproducible parquet bytes
    rows: list[dict] = []
    for name, current in PRODUCTS:
        rows.extend(history_for(name, current, rng))

    out = Path(__file__).parent / "product_price_history.parquet"
    con = duckdb.connect()
    # Stage the rows in a typed DuckDB table so the parquet schema
    # is explicit (DATE, DECIMAL(18,2)) instead of whatever the
    # Python types coerce to.
    con.execute("""
        CREATE TABLE staging (
            product_name   VARCHAR        NOT NULL,
            valid_from     DATE           NOT NULL,
            valid_to       DATE,
            unit_price     DECIMAL(18, 2) NOT NULL,
            change_reason  VARCHAR        NOT NULL
        )
    """)
    con.executemany(
        "INSERT INTO staging VALUES (?, ?, ?, ?, ?)",
        [(r["product_name"], r["valid_from"], r["valid_to"],
          r["unit_price"], r["change_reason"]) for r in rows],
    )
    con.execute(
        f"COPY staging TO '{out}' (FORMAT PARQUET, COMPRESSION 'snappy')"
    )
    print(f"wrote {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
