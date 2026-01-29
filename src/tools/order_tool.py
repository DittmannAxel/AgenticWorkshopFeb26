"""Order management tools for tracking and status queries.

These tools allow the agent to look up order status and history.

Example scenario (German):
    Kunde: "Wo ist meine Bestellung?"
    Agent: → get_recent_orders(customer_id="C-1001")
           → "Ihre Bestellung wird morgen zwischen 10-14 Uhr geliefert."
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import Field

from .base_tool import MockBackendClient

logger = logging.getLogger(__name__)


def get_recent_orders(
    customer_id: Annotated[str, Field(description="Customer ID to look up orders for")]
) -> dict:
    """Get recent orders for a customer. Use this to find a customer's order history."""
    logger.info("Orders lookup for customer: %s", customer_id)
    orders = [
        order for order in MockBackendClient.ORDERS.values()
        if order["customer_id"] == customer_id
    ]
    return {
        "customer_id": customer_id,
        "total_orders": len(orders),
        "orders": orders,
    }


def get_order_status(
    order_id: Annotated[str, Field(description="Order ID to check, e.g. ORD-5001")]
) -> dict:
    """Get the current status of a specific order. Use this when the customer asks about a specific order."""
    logger.info("Order status: %s", order_id)
    order = MockBackendClient.ORDERS.get(order_id)
    if order:
        return order
    return {"error": f"Order {order_id} not found."}
