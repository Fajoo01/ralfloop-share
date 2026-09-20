from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from .models import Offer, Product


_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _norm_text(value: str | None) -> str | None:
    if not value:
        return None
    text = _NON_ALNUM_RE.sub(" ", value.casefold()).strip()
    return _SPACE_RE.sub(" ", text) or None


def _norm_gtin(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits if 8 <= len(digits) <= 14 else None


def _product_id(identity_key: str) -> str:
    return "prd_" + hashlib.sha256(identity_key.encode()).hexdigest()[:20]


def identity_candidates(offer: Offer) -> list[tuple[str, float, str]]:
    candidates: list[tuple[str, float, str]] = []
    gtin = _norm_gtin(offer.gtin)
    if gtin:
        candidates.append((f"gtin:{gtin}", 0.995, f"GTIN={gtin}"))
    mpn = _norm_text(offer.mpn)
    brand = _norm_text(offer.brand)
    if mpn:
        key = f"mpn:{brand or '*'}:{mpn}"
        candidates.append((key, 0.97 if brand else 0.94, f"MPN={offer.mpn}"))
    model = _norm_text(offer.model)
    if model:
        key = f"model:{brand or '*'}:{model}"
        candidates.append((key, 0.93 if brand else 0.88, f"model={offer.model}"))
    return candidates


def _fallback_key(offer: Offer) -> tuple[str, float, str]:
    brand = _norm_text(offer.brand)
    title = _norm_text(offer.title) or offer.source_listing_id.casefold()
    return f"title:{brand or '*'}:{title}", 0.60, "normalized title fallback"


def _same_brand(left: str | None, right: str | None) -> bool:
    a = _norm_text(left)
    b = _norm_text(right)
    return not a or not b or a == b


def _product_keys(product: Product) -> set[str]:
    keys = {product.identity_key}
    gtin = _norm_gtin(product.gtin)
    brand = _norm_text(product.brand)
    mpn = _norm_text(product.mpn)
    model = _norm_text(product.model)
    if gtin:
        keys.add(f"gtin:{gtin}")
    if mpn:
        keys.add(f"mpn:{brand or '*'}:{mpn}")
    if model:
        keys.add(f"model:{brand or '*'}:{model}")
    return keys


class ProductResolver:
    def __init__(self, existing: list[Product] | None = None) -> None:
        self.products: dict[str, Product] = {product.id: product for product in (existing or [])}

    def resolve(self, offer: Offer) -> Product:
        candidates = identity_candidates(offer)
        candidate_map = {key: (score, why) for key, score, why in candidates}
        for product in self.products.values():
            shared = _product_keys(product).intersection(candidate_map)
            if shared:
                key = max(shared, key=lambda item: candidate_map[item][0])
                confidence, evidence = candidate_map[key]
                return self._bind(offer, product, confidence, [evidence])
        if not candidates:
            fuzzy = self._fuzzy_match(offer)
            if fuzzy is not None:
                product, score = fuzzy
                return self._bind(offer, product, score, [f"title_similarity={score:.3f}"])
        key, confidence, evidence = candidates[0] if candidates else _fallback_key(offer)
        product = Product(
            id=_product_id(key),
            title=offer.title,
            brand=offer.brand,
            gtin=_norm_gtin(offer.gtin),
            mpn=offer.mpn,
            model=offer.model,
            identity_key=key,
            identity_confidence=confidence,
            identity_evidence=[evidence],
        )
        self.products[product.id] = product
        return self._bind(offer, product, confidence, [evidence])

    def _fuzzy_match(self, offer: Offer) -> tuple[Product, float] | None:
        title = _norm_text(offer.title)
        if not title:
            return None
        best: tuple[Product, float] | None = None
        for product in self.products.values():
            if any((product.gtin, product.mpn, product.model)):
                continue
            if not _same_brand(offer.brand, product.brand):
                continue
            other = _norm_text(product.title)
            if not other:
                continue
            score = SequenceMatcher(None, title, other).ratio()
            if score >= 0.94 and (best is None or score > best[1]):
                best = (product, score)
        return best

    @staticmethod
    def _bind(offer: Offer, product: Product, confidence: float, evidence: list[str]) -> Product:
        offer.canonical_product_id = product.id
        offer.identity_confidence = confidence
        offer.identity_evidence = evidence
        return product
