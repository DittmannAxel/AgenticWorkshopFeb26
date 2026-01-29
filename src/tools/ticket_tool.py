"""Support ticket tools for complaint and issue management.

These tools allow the agent to create and track support tickets.

Example scenario (German):
    Kunde: "Meine Lieferung war beschädigt."
    Agent: → create_ticket(priority="high", category="damaged_delivery", ...)
           → "Ich habe ein Ticket erstellt. Ein Mitarbeiter meldet sich innerhalb 24h."
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from pydantic import Field

from .base_tool import MockBackendClient

logger = logging.getLogger(__name__)


def create_ticket(
    customer_id: Annotated[str, Field(description="Customer ID for the ticket")],
    category: Annotated[str, Field(description="Ticket category: damaged_delivery, missing_item, wrong_item, service_complaint, other")],
    priority: Annotated[str, Field(description="Priority level: low, medium, high, urgent")],
    description: Annotated[str, Field(description="Detailed description of the customer's issue")],
) -> dict:
    """Create a new support ticket for a customer issue. Use this for complaints, damaged deliveries, or other problems that need follow-up."""
    ticket_id = MockBackendClient.next_ticket_id()
    logger.info("Ticket created: %s (priority=%s, category=%s)", ticket_id, priority, category)

    # Determine SLA based on priority
    sla_hours = {"urgent": 4, "high": 24, "medium": 48, "low": 72}
    response_time = sla_hours.get(priority, 48)

    return {
        "success": True,
        "ticket_id": ticket_id,
        "customer_id": customer_id,
        "category": category,
        "priority": priority,
        "description": description,
        "created_at": datetime.now().isoformat(),
        "expected_response_hours": response_time,
        "message": f"Ticket {ticket_id} created. Expected response within {response_time} hours.",
    }


def get_ticket_status(
    ticket_id: Annotated[str, Field(description="Ticket ID to check, e.g. TKT-7001")]
) -> dict:
    """Check the status of an existing support ticket."""
    logger.info("Ticket status: %s", ticket_id)
    # In production, this would query the ticketing system
    return {
        "ticket_id": ticket_id,
        "status": "open",
        "message": f"Ticket {ticket_id} is currently being reviewed by our support team.",
    }
