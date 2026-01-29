"""Voice Live client using the official azure-ai-voicelive SDK.

This is the **recommended** approach for connecting to the Voice Live API.
The SDK handles connection management, authentication, and event typing.

For a lower-level WebSocket approach, see voice_live_client.py.

Docs:
- Quickstart: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart
- Agent Integration: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import AsyncIterator

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioNoiseReduction,
    AzureStandardVoice,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
)
from azure.identity.aio import DefaultAzureCredential

from .config import VoiceAgentConfig

logger = logging.getLogger(__name__)


class VoiceLiveSDKClient:
    """Voice Live client using the official Azure SDK.

    This client uses ``azure.ai.voicelive.aio.connect()`` which provides:
    - Typed event objects (no raw JSON parsing)
    - Built-in authentication handling
    - Automatic reconnection support
    - Proper async context manager

    Usage::

        config = VoiceAgentConfig()
        client = VoiceLiveSDKClient(config)

        async with client:
            # Stream audio
            await client.send_audio(audio_chunk)

            # Process events
            async for event in client.events():
                if event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
                    play_audio(base64.b64decode(event.delta))
    """

    def __init__(self, config: VoiceAgentConfig) -> None:
        self._config = config
        self._connection = None
        self._credential = DefaultAzureCredential()

    async def connect(self) -> None:
        """Open the Voice Live connection using the SDK.

        The SDK connect() function handles:
        - WebSocket connection with proper auth headers
        - Session creation and the initial handshake
        """
        endpoint = self._config.endpoint
        model = self._config.model_deployment

        logger.info("Connecting to Voice Live via SDK (endpoint=%s, model=%s)", endpoint, model)

        self._connection = await connect(
            endpoint=endpoint,
            credential=self._credential,
            model=model,
        ).__aenter__()

        # Configure the session with voice, VAD, and noise suppression
        voice_cfg = self._config.voice
        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            voice=AzureStandardVoice(
                name=voice_cfg.voice_name,
                temperature=voice_cfg.voice_temperature,
            ),
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=ServerVad(
                threshold=voice_cfg.vad_threshold,
                silence_duration_ms=voice_cfg.silence_duration_ms,
            ),
            input_audio_noise_reduction=AudioNoiseReduction(
                type=voice_cfg.noise_reduction_type,
            ),
        )

        await self._connection.session.update(session=session_config)
        logger.info("Voice Live session configured (voice=%s)", voice_cfg.voice_name)

    async def disconnect(self) -> None:
        """Close the Voice Live connection."""
        if self._connection:
            await self._connection.__aexit__(None, None, None)
            self._connection = None
        await self._credential.close()
        logger.info("Disconnected from Voice Live")

    async def __aenter__(self) -> VoiceLiveSDKClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send a chunk of PCM16 audio to Voice Live.

        The SDK's input_audio_buffer.append() accepts base64-encoded audio.
        """
        assert self._connection is not None, "Not connected"
        encoded = base64.b64encode(audio_bytes).decode("utf-8")
        await self._connection.input_audio_buffer.append(audio=encoded)

    async def create_response(self) -> None:
        """Trigger a response from the agent (e.g., for a proactive greeting)."""
        assert self._connection is not None, "Not connected"
        await self._connection.response.create()

    async def events(self) -> AsyncIterator:
        """Iterate over server events from Voice Live.

        Yields typed event objects. Common event types:
        - ServerEventType.SESSION_CREATED
        - ServerEventType.SESSION_UPDATED
        - ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED
        - ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED
        - ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
        - ServerEventType.RESPONSE_AUDIO_DELTA
        - ServerEventType.RESPONSE_TEXT_DONE
        - ServerEventType.RESPONSE_DONE
        - ServerEventType.ERROR
        """
        assert self._connection is not None, "Not connected"
        async for event in self._connection:
            yield event
