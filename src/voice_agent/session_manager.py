"""Session manager that orchestrates Voice Live and Agent Service.

Bridges the real-time audio processing of Voice Live API with the
conversational intelligence of the Foundry Agent Service.

Architecture:
    Customer Audio → Voice Live (STT) → Session Manager → Agent (LLM + Tools) → Session Manager → Voice Live (TTS) → Customer Audio

Docs:
- Voice Live + Agents: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

from .config import VoiceAgentConfig
from .voice_live_client import VoiceLiveClient
from .agent_client import FoundryAgentClient

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """Lifecycle states for a voice agent session."""
    IDLE = "idle"
    CONNECTING = "connecting"
    ACTIVE = "active"
    PROCESSING = "processing"
    DISCONNECTED = "disconnected"


@dataclass
class ConversationContext:
    """Tracks per-session conversation state."""
    session_id: str = ""
    thread_id: str = ""
    turn_count: int = 0
    customer_id: str | None = None
    transcript_history: list[dict[str, str]] = field(default_factory=list)

    def add_turn(self, role: str, text: str) -> None:
        """Record a conversation turn."""
        self.turn_count += 1
        self.transcript_history.append({
            "turn": self.turn_count,
            "role": role,
            "text": text,
        })


class SessionManager:
    """Orchestrates a single voice agent session.

    Manages the full lifecycle of a customer call:
    1. Establish Voice Live WebSocket connection
    2. Create an Agent conversation thread
    3. Route transcribed speech to the agent
    4. Route agent responses back to Voice Live for TTS
    5. Handle tool calls and interruptions

    Usage::

        config = VoiceAgentConfig()
        tools = [crm_tool, calendar_tool, order_tool, ticket_tool]

        session = SessionManager(config, tools)
        await session.start()

        # Stream audio from the customer
        await session.handle_audio(audio_chunk)

        # Graceful shutdown
        await session.stop()
    """

    def __init__(self, config: VoiceAgentConfig, tools: list | None = None) -> None:
        self._config = config
        self._voice_client = VoiceLiveClient(config)
        self._agent_client = FoundryAgentClient(config, tools=tools)
        self._context = ConversationContext()
        self._state = SessionState.IDLE
        self._processing_lock = asyncio.Lock()

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def context(self) -> ConversationContext:
        return self._context

    async def start(self) -> None:
        """Initialize the session: connect Voice Live and create agent thread."""
        self._state = SessionState.CONNECTING
        logger.info("Starting voice agent session...")

        # Initialize the agent (creates the agent in Foundry)
        await self._agent_client.initialize()

        # Create a conversation thread for this session
        self._context.thread_id = self._agent_client.create_thread()

        # Connect to Voice Live API
        await self._voice_client.connect()

        # Register event handlers for Voice Live events
        self._voice_client.on(
            "conversation.item.input_audio.transcription.completed",
            self._on_transcription_completed,
        )
        self._voice_client.on("session.created", self._on_session_created)
        self._voice_client.on("error", self._on_error)

        self._state = SessionState.ACTIVE
        logger.info("Voice agent session active (thread=%s)", self._context.thread_id)

    async def stop(self) -> None:
        """Gracefully shut down the session."""
        logger.info("Stopping voice agent session...")
        await self._voice_client.disconnect()
        await self._agent_client.cleanup()
        self._state = SessionState.DISCONNECTED
        logger.info(
            "Session ended after %d turns", self._context.turn_count
        )

    async def handle_audio(self, audio_bytes: bytes) -> None:
        """Forward incoming audio to Voice Live for processing.

        Voice Live handles:
        - Noise suppression (azure_deep_noise_suppression)
        - Voice activity detection (azure_semantic_vad)
        - Speech-to-text transcription
        """
        if self._state != SessionState.ACTIVE:
            logger.warning("Cannot send audio in state %s", self._state)
            return
        await self._voice_client.send_audio(audio_bytes)

    async def send_text(self, text: str) -> None:
        """Send a text message directly to Voice Live (bypasses STT).

        Useful for testing and demos where you want to simulate
        voice input without an actual microphone.
        """
        if self._state != SessionState.ACTIVE:
            logger.warning("Cannot send text in state %s", self._state)
            return
        await self._voice_client.send_text(text)

    # -- Voice Live Event Handlers -----------------------------------------

    async def _on_session_created(self, event: dict) -> None:
        """Handle session.created event from Voice Live."""
        session = event.get("session", {})
        self._context.session_id = session.get("id", "")
        logger.info("Voice Live session created: %s", self._context.session_id)

    async def _on_transcription_completed(self, event: dict) -> None:
        """Handle completed speech transcription from Voice Live.

        This is the main bridge between Voice Live and the Agent:
        1. Receive the transcribed text
        2. Send it to the Foundry Agent for processing
        3. Send the agent's response back to Voice Live for TTS
        """
        transcript = event.get("transcript", "")
        if not transcript.strip():
            return

        logger.info("Customer said: %s", transcript)
        self._context.add_turn("customer", transcript)

        # Prevent concurrent processing (one utterance at a time)
        async with self._processing_lock:
            self._state = SessionState.PROCESSING

            # Send transcript to the Foundry Agent
            # The agent will classify intent, call tools, and formulate a response
            response = await asyncio.to_thread(
                self._agent_client.process_message,
                self._context.thread_id,
                transcript,
            )

            self._context.add_turn("agent", response)
            logger.info("Agent response: %s", response[:100])

            # Send the agent's text response to Voice Live for TTS
            await self._voice_client.send_agent_response(response)

            self._state = SessionState.ACTIVE

    async def _on_error(self, event: dict) -> None:
        """Handle errors from Voice Live."""
        error = event.get("error", {})
        logger.error(
            "Voice Live error: code=%s, message=%s",
            error.get("code"),
            error.get("message"),
        )
