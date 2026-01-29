"""Voice Live WebSocket client for real-time speech processing.

Connects to the Azure Voice Live API to handle:
- Audio input streaming (microphone / telephony)
- Speech-to-text (STT) via Azure Speech
- Text-to-speech (TTS) with configurable voice
- Voice Activity Detection (VAD) with semantic understanding
- Deep Noise Suppression

Docs:
- Overview: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live
- Quickstart: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart
- API Reference: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-api-reference
- How-To: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Callable, Awaitable

import websockets
from websockets.asyncio.client import ClientConnection

from .config import VoiceAgentConfig

logger = logging.getLogger(__name__)

# Type alias for event handler callbacks
EventHandler = Callable[[dict], Awaitable[None]]


class VoiceLiveClient:
    """Async WebSocket client for the Azure Voice Live API.

    Usage::

        config = VoiceAgentConfig()
        client = VoiceLiveClient(config)

        client.on("conversation.item.input_audio.transcription.completed", handle_transcript)
        client.on("response.audio.delta", handle_audio_output)

        async with client:
            await client.send_audio(audio_chunk)
    """

    def __init__(self, config: VoiceAgentConfig) -> None:
        self._config = config
        self._ws: ClientConnection | None = None
        self._handlers: dict[str, list[EventHandler]] = {}
        self._receive_task: asyncio.Task | None = None

    # -- Lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket connection and configure the session."""
        url = self._config.voice_live_ws_url
        logger.info("Connecting to Voice Live API: %s", url)

        # Connect with Azure API key or token header
        self._ws = await websockets.connect(url, additional_headers=self._auth_headers())

        # Start background receiver loop
        self._receive_task = asyncio.create_task(self._receive_loop())

        # Send initial session configuration
        await self._send_event("session.update", {
            "session": self._config.voice.to_session_config()
        })
        logger.info("Voice Live session configured (voice=%s)", self._config.voice.voice_name)

    async def disconnect(self) -> None:
        """Close the WebSocket connection gracefully."""
        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("Disconnected from Voice Live API")

    async def __aenter__(self) -> VoiceLiveClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # -- Sending -----------------------------------------------------------

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Stream a chunk of PCM16 audio to Voice Live for processing.

        The audio is base64-encoded and sent as an input_audio_buffer.append event.
        Voice Live handles VAD, noise suppression, and STT automatically.
        """
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        await self._send_event("input_audio_buffer.append", {
            "audio": encoded,
        })

    async def send_text(self, text: str) -> None:
        """Send a text message to be processed by the agent (bypass STT)."""
        await self._send_event("conversation.item.create", {
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        })

    async def send_agent_response(self, text: str) -> None:
        """Inject the agent's text response for TTS rendering.

        After the Foundry Agent produces a text reply, send it here
        so Voice Live converts it to speech audio.
        """
        await self._send_event("conversation.item.create", {
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            }
        })
        # Trigger TTS generation
        await self._send_event("response.create", {})

    async def commit_audio_buffer(self) -> None:
        """Signal that the current audio buffer is complete (end of utterance)."""
        await self._send_event("input_audio_buffer.commit", {})

    # -- Event handling ----------------------------------------------------

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for a specific Voice Live event type.

        Common events:
        - session.created
        - conversation.item.input_audio.transcription.completed
        - response.audio.delta
        - response.done
        - error
        """
        self._handlers.setdefault(event_type, []).append(handler)

    # -- Receiving (internal) ----------------------------------------------

    async def _receive_loop(self) -> None:
        """Background loop that reads events from the WebSocket."""
        assert self._ws is not None
        try:
            async for message in self._ws:
                event = json.loads(message)
                event_type = event.get("type", "unknown")
                logger.debug("Voice Live event: %s", event_type)
                await self._dispatch(event_type, event)
        except websockets.ConnectionClosed:
            logger.info("Voice Live WebSocket connection closed")
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, event_type: str, event: dict) -> None:
        """Dispatch an event to all registered handlers."""
        for handler in self._handlers.get(event_type, []):
            try:
                await handler(event)
            except Exception:
                logger.exception("Error in handler for event '%s'", event_type)

    # -- Helpers -----------------------------------------------------------

    async def _send_event(self, event_type: str, payload: dict) -> None:
        """Serialize and send a JSON event over the WebSocket."""
        assert self._ws is not None, "Not connected"
        message = {"type": event_type, **payload}
        await self._ws.send(json.dumps(message))
        logger.debug("Sent event: %s", event_type)

    def _auth_headers(self) -> dict[str, str]:
        """Build authentication headers for the WebSocket handshake.

        Uses API key if AZURE_API_KEY is set, otherwise falls back
        to DefaultAzureCredential bearer token.
        """
        import os
        api_key = os.getenv("AZURE_API_KEY")
        if api_key:
            return {"api-key": api_key}

        # Use Azure Identity for token-based auth
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        return {"Authorization": f"Bearer {token.token}"}
