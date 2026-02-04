# -------------------------------------------------------------------------
# Step 2: Voice Live API + Foundry Agent Integration (VoiceLive calls agent)
# -------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import base64
import logging
import os
import queue
import signal
import sys
from datetime import datetime
from typing import Optional, Union, TYPE_CHECKING, cast

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential, DefaultAzureCredential

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureStandardVoice,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
    AudioInputTranscriptionOptions,
)

from dotenv import load_dotenv
import pyaudio

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection

# ---------------------------------------------------------------------------
# Environment & Logging
# ---------------------------------------------------------------------------
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(str(os.path.join(os.path.dirname(__file__), "..", ".env")), override=True)

if not os.path.exists("logs"):
    os.makedirs("logs")

timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
log_format = "%(asctime)s:%(name)s:%(levelname)s:%(message)s"

logging.basicConfig(
    filename=f"logs/{timestamp}_agent_tools.log",
    filemode="w",
    format=log_format,
    level=log_level,
)

# Errors-only log for easier analysis
error_log_path = f"logs/{timestamp}_agent_tools_errors.log"
error_file_handler = logging.FileHandler(error_log_path, mode="w")
error_file_handler.setLevel(logging.ERROR)
error_file_handler.setFormatter(logging.Formatter(log_format))
logging.getLogger().addHandler(error_file_handler)

# Also log to console for faster debugging
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(log_format))
console_handler.setLevel(log_level)
logging.getLogger().addHandler(console_handler)
logger = logging.getLogger(__name__)


class AudioProcessor:
    """Handles real-time audio capture and playback via PyAudio."""

    loop: asyncio.AbstractEventLoop

    class AudioPlaybackPacket:
        def __init__(self, seq_num: int, data: Optional[bytes]):
            self.seq_num = seq_num
            self.data = data

    def __init__(self, connection):
        self.connection = connection
        self.audio = pyaudio.PyAudio()
        self.format = pyaudio.paInt16
        self.channels = 1
        self.rate = 24000
        self.chunk_size = 1200  # 50ms
        self.input_stream = None
        self.playback_queue: queue.Queue[AudioProcessor.AudioPlaybackPacket] = (
            queue.Queue()
        )
        self.playback_base = 0
        self.next_seq_num = 0
        self.output_stream: Optional[pyaudio.Stream] = None

    def start_capture(self):
        logger.info("Starting microphone capture (rate=%s, chunk=%s)", self.rate, self.chunk_size)

        def _capture_callback(in_data, _frame_count, _time_info, _status_flags):
            audio_base64 = base64.b64encode(in_data).decode("utf-8")
            asyncio.run_coroutine_threadsafe(
                self.connection.input_audio_buffer.append(audio=audio_base64),
                self.loop,
            )
            return (None, pyaudio.paContinue)

        if self.input_stream:
            return
        self.loop = asyncio.get_event_loop()
        self.input_stream = self.audio.open(
            format=self.format,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk_size,
            stream_callback=_capture_callback,
        )

    def start_playback(self):
        logger.info("Starting audio playback")
        if self.output_stream:
            return
        remaining = bytes()

        def _playback_callback(_in_data, frame_count, _time_info, _status_flags):
            nonlocal remaining
            frame_count *= pyaudio.get_sample_size(pyaudio.paInt16)
            out = remaining[:frame_count]
            remaining = remaining[frame_count:]

            while len(out) < frame_count:
                try:
                    packet = self.playback_queue.get_nowait()
                except queue.Empty:
                    out = out + bytes(frame_count - len(out))
                    continue
                if not packet or not packet.data:
                    break
                if packet.seq_num < self.playback_base:
                    if len(remaining) > 0:
                        remaining = bytes()
                    continue
                num_to_take = frame_count - len(out)
                out = out + packet.data[:num_to_take]
                remaining = packet.data[num_to_take:]

            return (
                (out, pyaudio.paContinue)
                if len(out) >= frame_count
                else (out, pyaudio.paComplete)
            )

        self.output_stream = self.audio.open(
            format=self.format,
            channels=self.channels,
            rate=self.rate,
            output=True,
            frames_per_buffer=self.chunk_size,
            stream_callback=_playback_callback,
        )

    def _get_and_increase_seq_num(self):
        seq = self.next_seq_num
        self.next_seq_num += 1
        return seq

    def queue_audio(self, audio_data: Optional[bytes]) -> None:
        self.playback_queue.put(
            AudioProcessor.AudioPlaybackPacket(
                seq_num=self._get_and_increase_seq_num(), data=audio_data
            )
        )

    def skip_pending_audio(self):
        """Stops current audio playback immediately."""
        self.playback_base = self._get_and_increase_seq_num()

    def shutdown(self):
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None
        if self.output_stream:
            self.skip_pending_audio()
            self.queue_audio(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None
        if self.audio:
            self.audio.terminate()


class AgentVoiceAssistant:
    """VoiceLive + Foundry Agent integration (agent is called by VoiceLive)."""

    def __init__(
        self,
        endpoint: str,
        credential: Union[AzureKeyCredential, AsyncTokenCredential],
        agent_id: str,
        project_name: str,
        voice: str,
    ):
        self.endpoint = endpoint
        self.credential = credential
        self.agent_id = agent_id
        self.project_name = project_name
        self.voice = voice
        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.session_ready = False
        self.conversation_started = False
        self._active_response = False
        self._response_api_done = False

    async def start(self):
        """Start the voice assistant session."""
        try:
            logger.info(
                "Connecting to VoiceLive API with agent %s for project %s",
                self.agent_id,
                self.project_name,
            )

            # Get agent access token for Foundry Agent integration
            agent_cred = DefaultAzureCredential()
            agent_access_token = (await agent_cred.get_token("https://ai.azure.com/.default")).token
            await agent_cred.close()
            logger.info("Obtained agent access token")

            async with connect(
                endpoint=self.endpoint,
                credential=self.credential,
                query={
                    "agent-id": self.agent_id,
                    "agent-project-name": self.project_name,
                    "agent-access-token": agent_access_token,
                },
            ) as connection:
                self.connection = connection
                self.audio_processor = AudioProcessor(connection)

                await self._setup_session()
                self.audio_processor.start_playback()

                print("\n" + "=" * 60)
                print("STEP 2: VOICE LIVE + FOUNDRY AGENT")
                print("Speak into your microphone. Press Ctrl+C to exit.")
                print("=" * 60 + "\n")

                await self._process_events()
        finally:
            if self.audio_processor:
                self.audio_processor.shutdown()

    async def _setup_session(self):
        """Configure VoiceLive session."""
        voice_config = (
            AzureStandardVoice(name=self.voice) if "-" in self.voice else self.voice
        )

        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            voice=voice_config,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=ServerVad(
                threshold=0.5, prefix_padding_ms=300, silence_duration_ms=500
            ),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(
                type="azure_deep_noise_suppression"
            ),
            input_audio_transcription=AudioInputTranscriptionOptions(model="azure-speech"),
        )

        conn = self.connection
        assert conn is not None
        await conn.session.update(session=session_config)
        logger.info("Session configuration sent")

    async def _process_events(self):
        """Process events from VoiceLive."""
        conn = self.connection
        assert conn is not None
        async for event in conn:
            await self._handle_event(event)

    async def _handle_event(self, event):
        logger.debug("Event received: %s", event.type)
        ap = self.audio_processor
        conn = self.connection
        assert ap is not None
        assert conn is not None

        if event.type == ServerEventType.SESSION_UPDATED:
            logger.info("Session ready")
            self.session_ready = True
            if not self.conversation_started:
                self.conversation_started = True
                try:
                    await conn.response.create()
                except Exception:
                    logger.exception("Failed to send proactive greeting")
            ap.start_capture()

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            transcript = event.get("transcript", "")
            print(f"[You said] {transcript}")

        elif event.type == ServerEventType.RESPONSE_TEXT_DONE:
            text = event.get("text", "")
            print(f"[Agent] {text}")

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            transcript = event.get("transcript", "")
            print(f"[Agent audio transcript] {transcript}")

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            print("[Listening...]")
            ap.skip_pending_audio()
            if self._active_response and not self._response_api_done:
                try:
                    await conn.response.cancel()
                except Exception:
                    pass

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            print("[Processing...]")

        elif event.type == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_api_done = False

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            ap.queue_audio(event.delta)

        elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
            print("[Ready...]")

        elif event.type == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_api_done = True

        elif event.type == ServerEventType.ERROR:
            msg = event.error.message
            if "no active response" in msg.lower():
                logger.debug("Benign cancel error: %s", msg)
            else:
                logger.error("VoiceLive error: %s", msg)
                print(f"Error: {msg}")


def main():
    voicelive_endpoint = os.environ.get("AZURE_VOICELIVE_ENDPOINT")
    voicelive_api_key = os.environ.get("AZURE_VOICELIVE_API_KEY")
    voice = os.environ.get("AZURE_VOICELIVE_VOICE", "de-DE-ConradNeural")
    agent_id = os.environ.get("AZURE_VOICELIVE_AGENT_ID") or os.environ.get("AZURE_EXISTING_AGENT_ID")
    project_name = os.environ.get("AZURE_VOICELIVE_PROJECT_NAME") or os.environ.get("AZURE_AGENT_PROJECT")
    use_token_credential = (
        os.environ.get("USE_TOKEN_CREDENTIAL", "false").lower() == "true"
    )

    if not voicelive_endpoint:
        print("ERROR: AZURE_VOICELIVE_ENDPOINT is not set.")
        sys.exit(1)
    if not agent_id:
        print("ERROR: AZURE_VOICELIVE_AGENT_ID is not set.")
        sys.exit(1)
    if not project_name:
        print("ERROR: AZURE_VOICELIVE_PROJECT_NAME is not set.")
        sys.exit(1)

    credential: Union[AzureKeyCredential, AsyncTokenCredential]
    if use_token_credential:
        credential = AzureCliCredential()
        logger.info("Using Azure token credential for VoiceLive")
    else:
        if not voicelive_api_key:
            print("ERROR: AZURE_VOICELIVE_API_KEY is not set.")
            sys.exit(1)
        credential = AzureKeyCredential(voicelive_api_key)
        logger.info("Using API key credential for VoiceLive")

    assistant = AgentVoiceAssistant(
        endpoint=voicelive_endpoint,
        credential=credential,
        agent_id=agent_id,
        project_name=project_name,
        voice=voice,
    )

    def signal_handler(_sig, _frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(assistant.start())
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    try:
        p = pyaudio.PyAudio()
        input_devices = [
            i
            for i in range(p.get_device_count())
            if cast(Union[int, float], p.get_device_info_by_index(i).get("maxInputChannels", 0) or 0) > 0
        ]
        output_devices = [
            i
            for i in range(p.get_device_count())
            if cast(Union[int, float], p.get_device_info_by_index(i).get("maxOutputChannels", 0) or 0) > 0
        ]
        p.terminate()

        if not input_devices:
            print("❌ No audio input devices found. Please check your microphone.")
            sys.exit(1)
        if not output_devices:
            print("❌ No audio output devices found. Please check your speakers.")
            sys.exit(1)

    except Exception as e:
        print(f"❌ Audio system check failed: {e}")
        sys.exit(1)

    print("🎙️  VoiceLive + Foundry Agent")
    print("=" * 50)
    main()
