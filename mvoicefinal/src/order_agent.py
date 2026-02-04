from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from src.order_backend import OrderBackend, extract_order_id


class OrderAgentActionType(str, Enum):
    ASK_IDENTIFIER = "ask_identifier"
    LOOKUP = "lookup"
    PASS_THROUGH = "pass_through"


@dataclass
class OrderLookupRequest:
    order_id: Optional[str] = None
    customer_name: Optional[str] = None


@dataclass
class OrderAgentAction:
    type: OrderAgentActionType
    say: Optional[str] = None
    lookup: Optional[OrderLookupRequest] = None


@dataclass
class OrderConversationState:
    awaiting_identifier: bool = False
    last_intent: Optional[str] = None
    last_customer_name: Optional[str] = None
    last_order_id: Optional[str] = None


_ORDER_INTENT_RE = re.compile(
    r"\b("
    r"order|orders|"
    r"bestellung|bestellungen|bestellen|bestellt|"
    r"lieferung|lieferstatus|lieferzeit|zustellung|"
    r"versand|sendung|paket|tracking|"
    r"status"
    r")\b",
    re.IGNORECASE,
)


def _maybe_extract_customer_name(text: str) -> Optional[str]:
    if not text:
        return None
    cleaned = text.strip()

    patterns = [
        r"(?:my name is|i am|this is)\s+([A-Za-zÄÖÜäöüß\-]+\s+[A-Za-zÄÖÜäöüß\-]+)\b",
        r"(?:ich bin|mein name ist)\s+([A-Za-zÄÖÜäöüß\-]+\s+[A-Za-zÄÖÜäöüß\-]+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # If user just says two words (e.g., "Max Mustermann"), assume that's the name.
    tokens = cleaned.split()
    if len(tokens) == 2 and all(t and t[0].isalpha() for t in tokens):
        return cleaned

    return None


class OrderAgent:
    """A deterministic, multi-turn agent for the order-status use case."""

    def __init__(self, backend: OrderBackend):
        self._backend = backend
        self._state = OrderConversationState()

    @property
    def state(self) -> OrderConversationState:
        return self._state

    async def decide(self, transcript: str) -> OrderAgentAction:
        text = (transcript or "").strip()
        if not text:
            return OrderAgentAction(type=OrderAgentActionType.PASS_THROUGH)

        order_id = extract_order_id(text)
        customer_name = _maybe_extract_customer_name(text)
        is_order_related = bool(_ORDER_INTENT_RE.search(text)) or self._state.awaiting_identifier

        if not is_order_related:
            return OrderAgentAction(type=OrderAgentActionType.PASS_THROUGH)

        # If we were waiting for an identifier, treat the next user turn as candidate info.
        if self._state.awaiting_identifier:
            self._state.awaiting_identifier = False

        if order_id:
            self._state.last_order_id = order_id
            return OrderAgentAction(
                type=OrderAgentActionType.LOOKUP,
                say="Einen Moment bitte, ich prüfe den Status Ihrer Bestellung.",
                lookup=OrderLookupRequest(order_id=order_id),
            )

        if customer_name:
            self._state.last_customer_name = customer_name
            return OrderAgentAction(
                type=OrderAgentActionType.LOOKUP,
                say="Danke. Einen Moment bitte, ich schaue Ihre letzten Bestellungen nach.",
                lookup=OrderLookupRequest(customer_name=customer_name),
            )

        self._state.awaiting_identifier = True
        return OrderAgentAction(
            type=OrderAgentActionType.ASK_IDENTIFIER,
            say=(
                "Gerne. Können Sie mir bitte Ihre Bestellnummer nennen, "
                "zum Beispiel ORD-<nummer>, oder alternativ Ihren Namen?"
            ),
        )

    async def lookup(self, request: OrderLookupRequest) -> dict[str, Any]:
        if request.order_id:
            return await self._backend.get_order_status(request.order_id)
        if request.customer_name:
            orders = await self._backend.find_recent_orders_by_customer_name(request.customer_name)
            return {"found": bool(orders), "orders": orders, "customer_name": request.customer_name}
        return {"found": False, "error": "No lookup parameters provided."}
