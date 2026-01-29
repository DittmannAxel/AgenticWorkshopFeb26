"""Customer service tools for the Foundry Agent.

Each tool follows the Foundry Agent function-calling pattern:
- Type-annotated parameters with Pydantic Field descriptions
- Docstrings used by the LLM to understand when to call the tool
- Returns structured data for the agent to formulate responses

Docs: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools-classic/overview
"""

from .crm_tool import identify_customer, get_customer_details
from .calendar_tool import check_availability, book_appointment
from .order_tool import get_recent_orders, get_order_status
from .ticket_tool import create_ticket, get_ticket_status

# All tool functions to register with the Foundry Agent
ALL_TOOLS = [
    identify_customer,
    get_customer_details,
    check_availability,
    book_appointment,
    get_recent_orders,
    get_order_status,
    create_ticket,
    get_ticket_status,
]
