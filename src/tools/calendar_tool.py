"""Calendar tools for appointment management.

These tools allow the agent to check availability and book appointments.

Example scenario (German):
    Kunde: "Ich möchte einen Termin für nächste Woche."
    Agent: → check_availability(date="2025-02-03")
           → book_appointment(date="2025-02-03", time="10:00", ...)
           → "Ich habe den Termin für Dienstag um 10 Uhr gebucht."
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import Field

from .base_tool import MockBackendClient

logger = logging.getLogger(__name__)


def check_availability(
    date: Annotated[str, Field(description="Date to check in YYYY-MM-DD format")]
) -> dict:
    """Check available appointment slots for a given date. Use this before booking an appointment."""
    logger.info("Calendar check availability: %s", date)
    slots = MockBackendClient.get_available_slots(date)
    available = [s for s in slots if s["available"]]
    return {
        "date": date,
        "available_slots": available,
        "total_available": len(available),
    }


def book_appointment(
    date: Annotated[str, Field(description="Appointment date in YYYY-MM-DD format")],
    time: Annotated[str, Field(description="Appointment time in HH:MM format, e.g. 10:00")],
    customer_id: Annotated[str, Field(description="Customer ID for the appointment")],
    reason: Annotated[str, Field(description="Reason or topic for the appointment")],
) -> dict:
    """Book an appointment at the specified date and time. Check availability first."""
    logger.info("Calendar book: %s %s for %s", date, time, customer_id)
    # In production, this would call the calendar API
    return {
        "success": True,
        "appointment_id": f"APT-{date.replace('-', '')}-{time.replace(':', '')}",
        "date": date,
        "time": time,
        "customer_id": customer_id,
        "reason": reason,
        "confirmation": f"Appointment confirmed for {date} at {time}.",
    }
