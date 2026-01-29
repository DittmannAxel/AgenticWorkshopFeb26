"""Tests for voice agent configuration."""

import os
from unittest import mock

import pytest

from src.voice_agent.config import VoiceAgentConfig, VoiceLiveConfig


class TestVoiceLiveConfig:
    """Tests for Voice Live session configuration."""

    def test_default_values(self):
        config = VoiceLiveConfig()
        assert config.input_audio_format == "pcm16"
        assert config.output_audio_format == "pcm16"
        assert config.input_audio_sampling_rate == 24000
        assert config.vad_type == "azure_semantic_vad"
        assert config.vad_threshold == 0.5
        assert config.silence_duration_ms == 500
        assert config.noise_reduction_type == "azure_deep_noise_suppression"

    def test_to_session_config(self):
        config = VoiceLiveConfig(voice_name="de-DE-ConradNeural")
        session = config.to_session_config()

        assert session["modalities"] == ["text", "audio"]
        assert session["voice"]["name"] == "de-DE-ConradNeural"
        assert session["voice"]["type"] == "azure-standard"
        assert session["voice"]["temperature"] == 0.8  # default per API docs
        assert session["input_audio_format"] == "pcm16"
        assert session["turn_detection"]["type"] == "azure_semantic_vad"
        assert session["input_audio_noise_reduction"]["type"] == "azure_deep_noise_suppression"

    def test_custom_values(self):
        config = VoiceLiveConfig(
            voice_name="en-US-JennyNeural",
            vad_threshold=0.8,
            silence_duration_ms=300,
        )
        session = config.to_session_config()
        assert session["voice"]["name"] == "en-US-JennyNeural"
        assert session["turn_detection"]["threshold"] == 0.8
        assert session["turn_detection"]["silence_duration_ms"] == 300


class TestVoiceAgentConfig:
    """Tests for the top-level voice agent configuration."""

    @mock.patch.dict(os.environ, {
        "AZURE_FOUNDRY_ENDPOINT": "https://myresource.services.ai.azure.com",
        "PROJECT_NAME": "my-project",
        "MODEL_DEPLOYMENT_NAME": "gpt-4.1",
    })
    def test_from_env(self):
        config = VoiceAgentConfig()
        assert config.endpoint == "https://myresource.services.ai.azure.com"
        assert config.project_name == "my-project"
        assert config.model_deployment == "gpt-4.1"

    @mock.patch.dict(os.environ, {
        "AZURE_FOUNDRY_ENDPOINT": "https://myresource.services.ai.azure.com",
        "PROJECT_NAME": "my-project",
    })
    def test_agent_endpoint(self):
        config = VoiceAgentConfig()
        assert config.agent_endpoint == (
            "https://myresource.services.ai.azure.com/api/projects/my-project"
        )

    @mock.patch.dict(os.environ, {
        "AZURE_FOUNDRY_ENDPOINT": "https://myresource.services.ai.azure.com",
        "PROJECT_NAME": "my-project",
    })
    def test_voice_live_ws_url(self):
        config = VoiceAgentConfig()
        url = config.voice_live_ws_url
        assert url.startswith("wss://")
        assert "voice-live/realtime" in url
        assert "api-version=" in url
        assert "model=gpt-4.1" in url

    def test_missing_endpoint_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                VoiceAgentConfig()
