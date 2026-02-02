"""Example 01: Basic Voice Live API Connection

Demonstrates how to:
- Connect to the Azure Voice Live WebSocket API
- Configure a session with German voice (de-DE-ConradNeural)
- Send audio and receive transcriptions
- Handle Voice Live events

Prerequisites:
- Azure AI Foundry resource with Speech Service enabled
- Set AZURE_FOUNDRY_ENDPOINT in .env

Docs:
- Quickstart: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart
- API Reference: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-api-reference
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.voice_agent.config import VoiceAgentConfig
from src.voice_agent.voice_live_client import VoiceLiveClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def on_session_created(event: dict) -> None:
    """Called when the Voice Live session is established."""
    session_id = event.get("session", {}).get("id", "")
    logger.info("Session created: %s", session_id)
    logger.info("Voice Live is ready to receive audio.")


async def on_transcription_completed(event: dict) -> None:
    """Called when Voice Live finishes transcribing an utterance."""
    transcript = event.get("transcript", "")
    logger.info("Transcription: %s", transcript)


async def on_audio_delta(event: dict) -> None:
    """Called when Voice Live sends TTS audio output."""
    # In a real app, you would play this audio or send it to a phone line
    logger.info("Received audio output (TTS)")


async def on_error(event: dict) -> None:
    """Called on Voice Live errors."""
    error = event.get("error", {})
    logger.error("Error: %s - %s", error.get("code"), error.get("message"))


async def main() -> None:
    """Run the basic Voice Live example."""
    # Load configuration from .env
    config = VoiceAgentConfig()
    logger.info("Connecting to: %s", config.voice_live_ws_url)
    logger.info("Voice: %s", config.voice.voice_name)

    # Create the Voice Live client
    client = VoiceLiveClient(config)

    # Register event handlers
    client.on("session.created", on_session_created)
    client.on("conversation.item.input_audio_transcription.completed", on_transcription_completed)
    client.on("response.audio.delta", on_audio_delta)
    client.on("error", on_error)

    # Connect and keep the session alive
    async with client:
        logger.info("Connected! In a real application, you would now stream audio.")
        logger.info("Press Ctrl+C to disconnect.")

        # Simulate sending a text message (bypassing audio/STT)
        await client.send_text("Hallo, ich möchte meinen Bestellstatus abfragen.")

        # Keep the connection alive
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    logger.info("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
