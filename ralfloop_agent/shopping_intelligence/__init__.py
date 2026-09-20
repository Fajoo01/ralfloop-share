"""Self-hosted shopping intelligence vertical slice."""

from .models import Offer, PriceObservation, Product, SearchRequest, SearchResponse
from .service import ShoppingService

__all__ = [
    "Offer",
    "PriceObservation",
    "Product",
    "SearchRequest",
    "SearchResponse",
    "ShoppingService",
]
