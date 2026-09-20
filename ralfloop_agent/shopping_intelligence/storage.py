from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import HistoryPoint, Offer, Product


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  brand TEXT,
  gtin TEXT,
  mpn TEXT,
  model TEXT,
  identity_key TEXT NOT NULL UNIQUE,
  identity_confidence REAL NOT NULL,
  identity_evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
  source TEXT NOT NULL,
  source_listing_id TEXT NOT NULL,
  canonical_product_id TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  condition TEXT,
  total_cost TEXT NOT NULL,
  currency TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY (source, source_listing_id)
);
"""
SCHEMA += """
CREATE TABLE IF NOT EXISTS price_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id TEXT NOT NULL,
  source TEXT NOT NULL,
  source_listing_id TEXT NOT NULL,
  total_cost TEXT NOT NULL,
  currency TEXT NOT NULL,
  condition TEXT,
  observed_at TEXT NOT NULL,
  raw_source_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_observations_product_time
  ON price_observations(product_id, observed_at);
"""


class ShoppingStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def list_products(self) -> list[Product]:
        rows = self.connection.execute("SELECT * FROM products ORDER BY id").fetchall()
        products: list[Product] = []
        for row in rows:
            products.append(
                Product(
                    id=row["id"],
                    title=row["title"],
                    brand=row["brand"],
                    gtin=row["gtin"],
                    mpn=row["mpn"],
                    model=row["model"],
                    identity_key=row["identity_key"],
                    identity_confidence=row["identity_confidence"],
                    identity_evidence=row["identity_evidence"].split("\n") if row["identity_evidence"] else [],
                )
            )
        return products

    def upsert_product(self, product: Product) -> None:
        self.connection.execute(
            """INSERT INTO products
            (id,title,brand,gtin,mpn,model,identity_key,identity_confidence,identity_evidence)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, brand=excluded.brand,
            gtin=excluded.gtin, mpn=excluded.mpn, model=excluded.model""",
            (
                product.id,
                product.title,
                product.brand,
                product.gtin,
                product.mpn,
                product.model,
                product.identity_key,
                product.identity_confidence,
                "\n".join(product.identity_evidence),
            ),
        )
        self.connection.commit()

    def persist_offer(self, offer: Offer) -> None:
        if not offer.canonical_product_id:
            raise ValueError("offer must be resolved before persistence")
        self.connection.execute(
            """INSERT INTO offers
            (source,source_listing_id,canonical_product_id,title,url,condition,total_cost,currency,observed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,source_listing_id) DO UPDATE SET
            canonical_product_id=excluded.canonical_product_id, title=excluded.title,
            url=excluded.url, condition=excluded.condition, total_cost=excluded.total_cost,
            currency=excluded.currency, observed_at=excluded.observed_at""",
            (
                offer.source,
                offer.source_listing_id,
                offer.canonical_product_id,
                offer.title,
                offer.url,
                offer.condition,
                str(offer.total_cost),
                offer.currency,
                offer.observed_at.isoformat(),
            ),
        )
        self.connection.execute(
            """INSERT INTO price_observations
            (product_id,source,source_listing_id,total_cost,currency,condition,observed_at,raw_source_hash)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                offer.canonical_product_id,
                offer.source,
                offer.source_listing_id,
                str(offer.total_cost),
                offer.currency,
                offer.condition,
                offer.observed_at.isoformat(),
                offer.raw_source_hash,
            ),
        )
        self.connection.commit()

    def history(self, product_id: str, limit: int = 500) -> list[HistoryPoint]:
        rows = self.connection.execute(
            """SELECT observed_at,source,source_listing_id,total_cost,currency,condition
            FROM price_observations WHERE product_id=?
            ORDER BY observed_at ASC LIMIT ?""",
            (product_id, limit),
        ).fetchall()
        return [
            HistoryPoint(
                observed_at=row["observed_at"],
                source=row["source"],
                source_listing_id=row["source_listing_id"],
                total_cost=row["total_cost"],
                currency=row["currency"],
                condition=row["condition"],
            )
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()
