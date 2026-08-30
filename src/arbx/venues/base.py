# Scope: SHARED_CORE — Adapter contract for public venue feeds.
from __future__ import annotations

from abc import ABC, abstractmethod

from arbx.core.models import OrderBook, VenueHealth


class AdapterContract(ABC):
    @abstractmethod
    def fetch_orderbook(self, market_id: str) -> OrderBook:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> VenueHealth:
        raise NotImplementedError
