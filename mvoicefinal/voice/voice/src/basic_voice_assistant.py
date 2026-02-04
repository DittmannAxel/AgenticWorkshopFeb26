# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# -------------------------------------------------------------------------
"""
BasicVoiceAssistant - CLI wrapper for standalone voice assistant usage.

This module provides a simple interface for running the voice assistant
from the command line with PyAudio for local microphone/speaker I/O.
"""
from __future__ import annotations

from typing import Union

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from dotenv import load_dotenv

from voice.src.audio_processor import AudioProcessor
from voice.src.voice_service import VoiceService, VoiceServiceConfig, VoiceEvent, VoiceEventType
from voice.src.set_logging import logger


# Environment variable loading
load_dotenv('./.env', override=True)


class BasicVoiceAssistant:
    """
    Basic voice assistant for standalone CLI usage.
    
    This is a thin wrapper around VoiceService that adds PyAudio-based
    microphone capture and speaker playback for local usage.
    """

    def __init__(
        self,
        endpoint: str,
        credential: Union[AzureKeyCredential, AsyncTokenCredential],
        model: str,
        voice: str,
        instructions: str,
    ):
        # Create voice service configuration
        config = VoiceServiceConfig(
            endpoint=endpoint,
            model=model,
            voice=voice,
            instructions=instructions,
        )
        
        self.voice_service = VoiceService(credential, config)
        self.audio_processor: AudioProcessor | None = None
        
        # Register event handlers
        self.voice_service.on_event(self._handle_voice_event)

    async def start(self):
        """Start the voice assistant session."""
        try:
            logger.info("Starting BasicVoiceAssistant")
            
            # Start voice service first
            await self.voice_service.start()
            
            # Initialize audio processor with connection from voice service
            self.audio_processor = AudioProcessor(self.voice_service.connection)
            
            # Start audio systems
            self.audio_processor.start_playback()
            self.audio_processor.start_capture()

            logger.info("Voice assistant ready! Start speaking...")
            print("\n" + "=" * 60)
            print("🎤 VOICE ASSISTANT READY")
            print("Start speaking to begin conversation")
            print("Press Ctrl+C to exit")
            print("=" * 60 + "\n")

            # Wait for voice service to complete (runs until stopped)
            event_task = self.voice_service.event_task
            if event_task:
                await event_task

        finally:
            await self.shutdown()

    async def shutdown(self):
        """Clean up resources."""
        if self.audio_processor:
            self.audio_processor.shutdown()
            self.audio_processor = None
        
        await self.voice_service.stop()

    async def _handle_voice_event(self, event: VoiceEvent) -> None:
        """Handle events from the voice service."""
        if event.type == VoiceEventType.SPEECH_STARTED:
            print("🎤 Listening...")
            if self.audio_processor:
                self.audio_processor.skip_pending_audio()

        elif event.type == VoiceEventType.SPEECH_ENDED:
            print("🤔 Processing...")

        elif event.type == VoiceEventType.RESPONSE_STARTED:
            pass  # Could add visual indicator

        elif event.type == VoiceEventType.RESPONSE_AUDIO:
            if self.audio_processor and event.data:
                audio_bytes = event.data.get("audio")
                if audio_bytes:
                    self.audio_processor.queue_audio(audio_bytes)

        elif event.type == VoiceEventType.RESPONSE_ENDED:
            print("🎤 Ready for next input...")

        elif event.type == VoiceEventType.TRANSCRIPT:
            if event.data:
                role = event.data.get("role", "unknown")
                transcript = event.data.get("transcript", "")
                if transcript:
                    prefix = "👤 You:" if role == "user" else "🤖 Assistant:"
                    print(f"{prefix} {transcript}")

        elif event.type == VoiceEventType.ERROR:
            if event.data:
                error = event.data.get("error", "Unknown error")
                print(f"❌ Error: {error}")

