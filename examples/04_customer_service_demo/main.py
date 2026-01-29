"""Example 04: Full Customer Service Voice Agent Demo

Complete demo application with:
- Interactive text console (simulating voice I/O)
- Full agent with all customer service tools
- Conversation state tracking
- Graceful shutdown

This is the most complete example, showing how a production-like
voice agent application would work.

Run with:
    python examples/04_customer_service_demo/main.py

Docs:
- Workshop: https://microsoft.github.io/build-your-first-agent-with-azure-ai-agent-service-workshop/
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


def print_banner() -> None:
    """Display the demo welcome banner."""
    print("\n" + "=" * 60)
    print("  Azure AI Foundry - Customer Service Voice Agent Demo")
    print("  (Text mode - simulating voice interaction)")
    print("=" * 60)
    print()
    print("This demo simulates a German customer service voice agent.")
    print("Type your messages as if speaking to the agent.")
    print("Type 'quit' or 'exit' to end the session.")
    print()
    print("Example prompts:")
    print('  - "Wo ist meine Bestellung?"')
    print('  - "Ich möchte einen Termin buchen."')
    print('  - "Meine Lieferung war beschädigt."')
    print()
    print("-" * 60)


async def main() -> None:
    """Run the interactive customer service demo."""
    print_banner()

    config = VoiceAgentConfig()
    agent = FoundryAgentClient(config, tools=ALL_TOOLS)

    print("Initializing agent...")
    await agent.initialize()
    thread_id = agent.create_thread()
    print("Agent ready!\n")

    turn_count = 0

    try:
        while True:
            # Read user input (simulating transcribed speech)
            user_input = input("Kunde: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "ende", "tschüss"):
                print("\nAgent: Vielen Dank für Ihren Anruf. Auf Wiederhören!")
                break

            turn_count += 1

            # Process through the agent (in production, this text comes from Voice Live STT)
            print("Agent: (verarbeitet...)")
            response = agent.process_message(thread_id, user_input)
            print(f"Agent: {response}\n")

    except (KeyboardInterrupt, EOFError):
        print("\n\nSession beendet.")
    finally:
        await agent.cleanup()
        print(f"\nGesamt-Turns: {turn_count}")
        print("Demo beendet.")


if __name__ == "__main__":
    asyncio.run(main())
