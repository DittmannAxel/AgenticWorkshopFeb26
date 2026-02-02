"""Configuration for the Azure AI Foundry Voice Agent.

Loads settings from environment variables or .env file.
See: https://learn.microsoft.com/en-us/azure/ai-foundry/quickstarts/get-started-code
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class VoiceLiveConfig:
    """Voice Live API session configuration.

    Docs: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live
    """

    voice_name: str = field(
        default_factory=lambda: (
            os.getenv("AZURE_VOICELIVE_VOICE")
            or os.getenv("VOICE_LIVE_VOICE", "de-DE-ConradNeural")
        )
    )
    api_version: str = field(
        default_factory=lambda: (
            os.getenv("AZURE_VOICELIVE_API_VERSION")
            or os.getenv("VOICE_LIVE_API_VERSION", "2025-10-01")
        )
    )
    transcription_model: str = field(
        default_factory=lambda: (
            os.getenv("AZURE_VOICELIVE_TRANSCRIPTION_MODEL")
            or os.getenv("VOICE_LIVE_TRANSCRIPTION_MODEL", "azure-speech")
        )
    )
    input_audio_format: str = "pcm16"
    output_audio_format: str = "pcm16"
    input_audio_sampling_rate: int = 24000
    vad_type: str = "azure_semantic_vad"
    vad_threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500
    noise_reduction_type: str = "azure_deep_noise_suppression"
    voice_temperature: float = 0.8  # Valid range: 0.6-1.2 per API docs

    def to_session_config(self) -> dict:
        """Build the Voice Live session configuration payload."""
        return {
            "modalities": ["text", "audio"],
            "voice": {
                "type": "azure-standard",
                "name": self.voice_name,
                "temperature": self.voice_temperature,
            },
            "input_audio_transcription": {
                "model": self.transcription_model,
            },
            "input_audio_format": self.input_audio_format,
            "output_audio_format": self.output_audio_format,
            "input_audio_sampling_rate": self.input_audio_sampling_rate,
            "turn_detection": {
                "type": self.vad_type,
                "threshold": self.vad_threshold,
                "prefix_padding_ms": self.prefix_padding_ms,
                "silence_duration_ms": self.silence_duration_ms,
            },
            "input_audio_noise_reduction": {
                "type": self.noise_reduction_type,
            },
        }


@dataclass
class VoiceAgentConfig:
    """Top-level configuration for the voice agent application.

    Docs:
    - Foundry: https://learn.microsoft.com/en-us/azure/ai-foundry/
    - Agents: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/overview
    """

    # Azure AI Foundry endpoint
    endpoint: str = field(
        default_factory=lambda: (
            os.getenv("AZURE_FOUNDRY_ENDPOINT")
            or os.getenv("AZURE_VOICELIVE_ENDPOINT")
            or os.environ["AZURE_FOUNDRY_ENDPOINT"]
        )
    )
    project_name: str = field(
        default_factory=lambda: os.environ["PROJECT_NAME"]
    )
    model_deployment: str = field(
        default_factory=lambda: os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4.1")
    )
    voice_live_model: str = field(
        default_factory=lambda: (
            os.getenv("AZURE_VOICELIVE_MODEL")
            or os.getenv("VOICE_LIVE_MODEL", "gpt-realtime")
        )
    )

    # Voice Live sub-config
    voice: VoiceLiveConfig = field(default_factory=VoiceLiveConfig)

    # Logging
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )

    @property
    def agent_endpoint(self) -> str:
        """Foundry Agent Service endpoint.

        Format: https://<resource>.services.ai.azure.com/api/projects/<project>
        Docs: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/quickstart
        """
        return f"{self.endpoint}/api/projects/{self.project_name}"

    @property
    def voice_live_ws_url(self) -> str:
        """Voice Live WebSocket URL (direct model mode).

        Format: wss://<resource>.services.ai.azure.com/voice-live/realtime
                ?api-version=2025-10-01&model=<voice-live-model>
        Docs: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart
        """
        return (
            f"wss://{self._host}/voice-live/realtime"
            f"?api-version={self.voice.api_version}"
            f"&model={self.voice_live_model}"
        )

    @property
    def _host(self) -> str:
        return self.endpoint.replace("https://", "").rstrip("/")
