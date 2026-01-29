"""Example 03: Voice Live + Agent Integration

Demonstrates the full integration:
- Voice Live handles audio I/O (STT + TTS)
- Foundry Agent processes customer intents and calls tools
- SessionManager orchestrates both components

This is the core pattern for building a voice-enabled AI agent.

Prerequisites:
- All Azure resources configured (see docs/setup-guide.md)
- Set all variables in .env

Docs:
- Agent Integration: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.voice_agent.config import VoiceAgentConfig
from src.voice_agent.session_manager import SessionManager
from src.tools import ALL_TOOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def simulate_audio_input(session: SessionManager) -> None:
    """Simulate audio input for demonstration.

    In production, audio would come from:
    - WebRTC browser connection
    - Telephony gateway (SIP/PSTN)
    - Microphone input
    """
    logger.info("In production, audio would be streamed here.")
    logger.info("The SessionManager handles the full flow:")
    logger.info("  Audio → Voice Live (STT) → Agent (LLM + Tools) → Voice Live (TTS) → Audio")

    # For demo: send text directly (bypasses STT)
    # This simulates what happens after Voice Live transcribes audio
    await session._voice_client.send_text(
        "Hallo, ich möchte meinen Bestellstatus abfragen. Meine Kundennummer ist C-1001."
    )

    # Wait for the agent to process and respond
    await asyncio.sleep(10)


async def main() -> None:
    """Run the integrated voice agent."""
    config = VoiceAgentConfig()

    # Create the session manager with all tools
    session = SessionManager(config, tools=ALL_TOOLS)

    logger.info("Starting Voice Agent session...")
    await session.start()

    try:
        logger.info("Session active. State: %s", session.state)
        await simulate_audio_input(session)
    finally:
        await session.stop()
        logger.info("Final state: %s", session.state)
        logger.info("Total turns: %d", session.context.turn_count)

        # Print conversation transcript
        if session.context.transcript_history:
            logger.info("Conversation transcript:")
            for turn in session.context.transcript_history:
                logger.info("  [%s] %s: %s", turn["turn"], turn["role"], turn["text"])


if __name__ == "__main__":
    asyncio.run(main())
