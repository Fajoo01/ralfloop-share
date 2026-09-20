from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    brand: str | None = None
    gtin: str | None = None
    mpn: str | None = None
    model: str | None = None
    identity_key: str
    identity_confidence: float = Field(ge=0, le=1)
    identity_evidence: list[str] = Field(default_factory=list)


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    source_listing_id: str
    title: str
    url: str
    condition: str | None = None
    seller: str | None = None
    seller_reputation: float | None = None
    item_price: Decimal
    shipping_cost: Decimal | None = None
    marketplace_fees: Decimal = Decimal("0")
    known_discount: Decimal = Decimal("0")
    unknown_cost_components: list[str] = Field(default_factory=list)
    total_cost: Decimal
    currency: str
    availability: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_source_hash: str
    brand: str | None = None
    gtin: str | None = None
    mpn: str | None = None
    model: str | None = None
    canonical_product_id: str | None = None
    identity_confidence: float | None = None
    identity_evidence: list[str] = Field(default_factory=list)


class PriceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    source: str
    source_listing_id: str
    total_cost: Decimal
    currency: str
    condition: str | None = None
    observed_at: datetime


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    urls: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=50)


class ConnectorResult(BaseModel):
    connector: str
    offers: list[Offer] = Field(default_factory=list)
    error: str | None = None


class SearchResponse(BaseModel):
    query: str
    products: list[Product]
    offers: list[Offer]
    connector_results: list[ConnectorResult]
    observations_written: int


class HistoryPoint(BaseModel):
    observed_at: datetime
    source: str
    source_listing_id: str
    total_cost: Decimal
    currency: str
    condition: str | None = None


JsonDict = dict[str, Any]
