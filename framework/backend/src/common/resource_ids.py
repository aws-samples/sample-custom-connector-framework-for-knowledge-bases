"""New resource IDs, and the clock DAOs date their items with. Built in each composition root."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ccf_api_models.identifiers import GeneratedIdKind

_ID_HEX = 12


class ResourceIds:
    def new(self, kind: GeneratedIdKind) -> str:
        """The kind's prefix and 12 random hex characters."""
        return f"{kind.value}{uuid.uuid4().hex[:_ID_HEX]}"


@runtime_checkable
class Clock(Protocol):
    def now(self) -> Decimal:
        """Epoch seconds, as the DynamoDB number a DAO writes."""
        ...


class SystemClock:
    def now(self) -> Decimal:
        return Decimal(str(time.time()))
