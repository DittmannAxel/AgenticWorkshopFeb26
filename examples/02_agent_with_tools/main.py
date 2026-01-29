"""Example 02: Foundry Agent with Tools (text-only, no voice)

Demonstrates how to:
- Create a Foundry Agent with customer service tools
- Run a text conversation with the agent
- See tool calls in action (CRM, Calendar, Orders, Tickets)

This example uses only the Agent Service (no Voice Live).
It shows how the agent processes customer requests and calls tools.

Prerequisites:
- Azure AI Foundry resource with a model deployment (e.g. gpt-4.1)
- Set AZURE_FOUNDRY_ENDPOINT and PROJECT_NAME in .env

Docs:
- Agent Quickstart: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/quickstart
- Python SDK: https://learn.microsoft.com/en-us/python/api/overview/azure/ai-agents-readme
- Tools: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools-classic/overview
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.voice_agent.config import VoiceAgentConfig
from src.voice_agent.agent_client import FoundryAgentClient
from src.tools import ALL_TOOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Sample customer service conversations (German)
CONVERSATIONS = [
    {
        "name": "Bestellstatus (Order Status)",
        "messages": [
            "Guten Tag, ich möchte wissen, wo meine Bestellung ist.",
            "Meine Kundennummer ist C-1001.",
            "Was ist mit der Bestellung ORD-5001?",
        ],
    },
    {
        "name": "Terminbuchung (Appointment Booking)",
        "messages": [
            "Ich möchte gerne einen Termin für nächste Woche buchen.",
            "Dienstag wäre gut. Was haben Sie frei?",
            "Bitte den 10-Uhr-Termin.",
        ],
    },
    {
        "name": "Beschwerde (Complaint)",
        "messages": [
            "Meine Lieferung war leider beschädigt.",
            "Es handelt sich um die Bestellung ORD-5002.",
            "Ja, bitte erstellen Sie ein Ticket.",
        ],
    },
]


async def run_conversation(agent: FoundryAgentClient, conversation: dict) -> None:
    """Run a single sample conversation with the agent."""
    logger.info("=" * 60)
    logger.info("Scenario: %s", conversation["name"])
    logger.info("=" * 60)

    # Create a new thread for each conversation
    thread_id = agent.create_thread()

    for message in conversation["messages"]:
        logger.info("Kunde: %s", message)
        response = agent.process_message(thread_id, message)
        logger.info("Agent: %s", response)
        logger.info("-" * 40)


async def main() -> None:
    """Run all sample conversations."""
    config = VoiceAgentConfig()
    agent = FoundryAgentClient(config, tools=ALL_TOOLS)

    logger.info("Initializing Foundry Agent with %d tools...", len(ALL_TOOLS))
    await agent.initialize()

    try:
        for conversation in CONVERSATIONS:
            await run_conversation(agent, conversation)
    finally:
        await agent.cleanup()

    logger.info("All conversations completed.")


if __name__ == "__main__":
    asyncio.run(main())
