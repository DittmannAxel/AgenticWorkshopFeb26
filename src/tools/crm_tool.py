"""CRM tools for customer identification and data retrieval.

These tools allow the agent to look up customer information
from the CRM system based on phone number or customer ID.

Example scenario (German):
    Kunde: "Wo ist meine Bestellung?"
    Agent: → identify_customer(phone="+49 170 1234567")
           → "Guten Tag, Frau Schmidt. Ich schaue gleich nach."
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import Field

from .base_tool import MockBackendClient

logger = logging.getLogger(__name__)


def identify_customer(
    phone: Annotated[str, Field(description="Customer phone number in international format, e.g. +49 170 1234567")]
) -> dict:
    """Identify a customer by their phone number. Use this when you need to look up who is calling."""
    logger.info("CRM lookup by phone: %s", phone)
    for customer in MockBackendClient.CUSTOMERS.values():
        if customer["phone"] == phone:
            return {
                "found": True,
                "customer_id": customer["id"],
                "name": customer["name"],
                "tier": customer["tier"],
            }
    return {"found": False, "message": "No customer found for this phone number."}


def get_customer_details(
    customer_id: Annotated[str, Field(description="The customer ID, e.g. C-1001")]
) -> dict:
    """Get detailed customer information by customer ID. Use this to retrieve full profile data."""
    logger.info("CRM get details: %s", customer_id)
    customer = MockBackendClient.CUSTOMERS.get(customer_id)
    if customer:
        return customer
    return {"error": f"Customer {customer_id} not found."}
