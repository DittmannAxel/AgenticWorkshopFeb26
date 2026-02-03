# -------------------------------------------------------------------------
# Step 2: Voice Live API + Local Agent Framework
# -------------------------------------------------------------------------
#
# In this example, VoiceLive handles ONLY the audio (STT + TTS).
# The local Agent Framework handles reasoning and response generation.
#
# Flow:
#   User speaks -> VoiceLive (STT) -> Transcription event
#     -> Your code sends text to the local Agent Framework
#     -> Agent Framework generates a response from the local dataset
#     -> Your code injects response into VoiceLive -> VoiceLive (TTS) -> User
#
# Key difference from Step 1:
#   - NO tools registered on the VoiceLive session
#   - Trigger is TRANSCRIPTION_COMPLETED, not FUNCTION_CALL
#   - Agent Framework runs locally (no cloud agent created)
# -------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Union

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential

# Voice Live SDK
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

# Local Agent Framework
from agent_framework.azure import AzureAIClient

from dotenv import load_dotenv
import pyaudio

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection

# ---------------------------------------------------------------------------
# Path setup: allow shared prompts from src/
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Environment & Logging
# ---------------------------------------------------------------------------
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"), override=True)

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

# Also log to console for faster debugging
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(log_format))
console_handler.setLevel(log_level)
logging.getLogger().addHandler(console_handler)
logger = logging.getLogger(__name__)


# Load system prompt (prefer example-specific prompt)
LOCAL_PROMPT_PATH = Path(__file__).resolve().parent / "agent_prompt.md"
PROMPTS_DIR = PROJECT_ROOT / "src" / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt.md"
DATA_PATH = Path(__file__).resolve().parent / "data" / "orders.json"


def load_system_prompt() -> str:
    if LOCAL_PROMPT_PATH.exists():
        return LOCAL_PROMPT_PATH.read_text(encoding="utf-8")
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        "Du bist ein freundlicher Kundenservice-Agent. "
        "Beantworte Kundenanfragen hoeflich und effizient auf Deutsch. "
        "Nutze die verfuegbaren Tools, um Kundendaten abzurufen, "
        "Termine zu buchen, Bestellungen zu pruefen und Tickets zu erstellen."
    )


def load_dataset() -> dict:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Dataset not found: {DATA_PATH}")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


# ============================================================
# AGENT BRIDGE: Local Agent Framework
# ============================================================


class AgentBridge:
    """Local Agent Framework bridge.

    Uses AzureAIClient locally (not Foundry Agent Service).
    The agent answers based on the provided dataset.
    """

    def __init__(self, project_endpoint: str, model: str, instructions: str):
        self.project_endpoint = project_endpoint
        self.model = model
        self.instructions = instructions
        self._credential: Optional[AzureCliCredential] = None
        self._client: Optional[AzureAIClient] = None
        self._agent = None

    async def initialize(self):
        self._credential = AzureCliCredential()
        self._client = AzureAIClient(
            async_credential=self._credential,
            azure_ai_project_endpoint=self.project_endpoint,
            azure_ai_model_deployment_name=self.model,
        )
        self._agent = self._client.as_agent(instructions=self.instructions)
        logger.info("Local Agent Framework initialized")

    async def process_message(self, user_text: str) -> str:
        assert self._agent is not None

        dataset = load_dataset()
        data_json = json.dumps(dataset, ensure_ascii=False, indent=2)
        message = f"Nutzeranfrage: {user_text}\n\nDATEN (JSON):\n{data_json}"

        result = await self._agent.run(message)
        response_text = getattr(result, "text", str(result))
        logger.info("Agent response: %s", response_text[:200])
        return response_text

    async def cleanup(self):
        if self._credential:
            await self._credential.close()


# ============================================================
# PENDING QUERY TRACKING
# ============================================================


class QueryState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INJECTED = "injected"


@dataclass
class PendingQuery:
    """Tracks a running background agent call."""

    query_id: str
    task: Optional[asyncio.Task] = None
    result: Optional[str] = None
    state: QueryState = QueryState.PENDING


# ============================================================
# AUDIO PROCESSOR
# ============================================================


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
        self.chunk_size = 1200
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


# ============================================================
# AGENT VOICE ASSISTANT
# ============================================================


class AgentVoiceAssistant:
    """
    Voice assistant where VoiceLive handles audio (STT/TTS) and the
    local Agent Framework handles reasoning and response generation.

    Key difference from Step 1:
      - VoiceLive session has NO tools registered
      - Transcription events trigger local agent calls
      - Agent Framework uses the local dataset to answer
      - Agent response is injected back for TTS
    """

    def __init__(
        self,
        voicelive_endpoint: str,
        voicelive_credential: Union[AzureKeyCredential, AsyncTokenCredential],
        voicelive_model: str,
        voice: str,
        project_endpoint: str,
        model_deployment: str,
    ):
        self.voicelive_endpoint = voicelive_endpoint
        self.voicelive_credential = voicelive_credential
        self.voicelive_model = voicelive_model
        self.voice = voice

        # Local Agent Framework bridge
        self.agent_bridge = AgentBridge(
            project_endpoint=project_endpoint,
            model=model_deployment,
            instructions=load_system_prompt(),
        )

        # VoiceLive instructions: minimal, just acknowledge and wait
        self.instructions = """Du bist eine Sprachschnittstelle fuer einen Kundenservice.

WICHTIG:
- Wenn der Nutzer etwas sagt, antworte KURZ mit einer Bestaetigung wie
  "Einen Moment bitte, ich schaue das fuer Sie nach."
- Halte dich kurz. Du wirst gleich die eigentliche Antwort erhalten.
- Antworte auf Deutsch in der Sie-Form.
- Falls eine Nachricht mit dem Praefix "[AGENT_RESPONSE]" beginnt, gib den Text
  nach dem Praefix wortwoertlich aus und fuege nichts hinzu."""
        self._agent_response_prefix = "[AGENT_RESPONSE]"

        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.session_ready = False
        self._active_response = False
        self._response_api_done = False
        self._pending_response_request = False
        self._resume_after_barge_in = False

        # Background agent queries
        self.pending_queries: Dict[str, PendingQuery] = {}
        self._result_checker_task: Optional[asyncio.Task] = None
        self._query_counter = 0

        # Processing lock to avoid concurrent agent calls
        self._processing_lock = asyncio.Lock()

    async def start(self):
        """Starts the Voice Assistant with local Agent Framework."""
        try:
            # Initialize the local Agent Framework
            print("Initializing local Agent Framework...")
            await self.agent_bridge.initialize()

            logger.info("Connecting to VoiceLive API...")

            async with connect(
                endpoint=self.voicelive_endpoint,
                credential=self.voicelive_credential,
                model=self.voicelive_model,
            ) as connection:
                self.connection = connection
                self.audio_processor = AudioProcessor(connection)

                await self._setup_session()
                self.audio_processor.start_playback()

                self._result_checker_task = asyncio.create_task(
                    self._check_for_completed_queries()
                )

                print("\n" + "=" * 60)
                print("STEP 2: VOICE LIVE + LOCAL AGENT FRAMEWORK")
                print("VoiceLive handles audio. Local agent handles reasoning.")
                print("Speak into your microphone. Press Ctrl+C to exit.")
                print("=" * 60 + "\n")

                await self._process_events()

        finally:
            if self._result_checker_task:
                self._result_checker_task.cancel()
            if self.audio_processor:
                self.audio_processor.shutdown()
            try:
                await self.agent_bridge.cleanup()
            except Exception:
                pass

    async def _setup_session(self):
        """Configures VoiceLive session -- NO tools, just audio + transcription."""

        voice_config = (
            AzureStandardVoice(name=self.voice) if "-" in self.voice else self.voice
        )

        # NOTE: No tools registered here! VoiceLive is audio-only.
        # The local Agent Framework handles all reasoning.
        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=self.instructions,
            voice=voice_config,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=ServerVad(
                threshold=0.65, prefix_padding_ms=300, silence_duration_ms=800
            ),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(
                type="azure_deep_noise_suppression"
            ),
            # Transcription is critical: this is how we get the user's text
            # to send to the local agent
            input_audio_transcription=AudioInputTranscriptionOptions(model="azure-speech"),
        )

        await self.connection.session.update(session=session_config)
        logger.info("VoiceLive session configured (no tools, transcription enabled)")

    async def _process_events(self):
        """Processes events from VoiceLive."""
        async for event in self.connection:
            await self._handle_event(event)

    async def _handle_event(self, event):
        """Event handler -- listens for transcription to trigger agent calls."""
        ap = self.audio_processor
        conn = self.connection

        if event.type == ServerEventType.SESSION_UPDATED:
            logger.info("Session ready")
            self.session_ready = True
            ap.start_capture()

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            print("[Listening...]")
            ap.skip_pending_audio()

            if self._active_response and not self._response_api_done:
                logger.info("Barge-in detected, canceling active response")
                self._resume_after_barge_in = True
                try:
                    await conn.response.cancel()
                except Exception:
                    pass

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            print("[Processing speech...]")

        elif event.type == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_api_done = False

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            if event.delta:
                logger.debug("Audio delta bytes: %d", len(event.delta))
            ap.queue_audio(event.delta)

        elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
            print("[Ready...]")

        elif event.type == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_api_done = True

            if self._pending_response_request:
                self._pending_response_request = False
                logger.info("Pending response detected, requesting response now")
                await conn.response.create()

        # ============================================================
        # THIS IS THE KEY EVENT: User's speech has been transcribed.
        # Instead of letting VoiceLive handle tool calls (Step 1),
        # we send the transcript to the local agent.
        # ============================================================
        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            transcript = event.transcript
            if transcript and transcript.strip():
                print(f"[Transcript: {transcript.strip()}]")
                logger.info("Transcription: %s", transcript.strip())
                self._resume_after_barge_in = False

                # Send to agent in background
                self._query_counter += 1
                query_id = f"agent_{self._query_counter}"

                pending = PendingQuery(
                    query_id=query_id,
                    state=QueryState.RUNNING,
                )
                pending.task = asyncio.create_task(
                    self._process_with_agent(query_id, transcript.strip())
                )
                self.pending_queries[query_id] = pending
            else:
                if self._resume_after_barge_in:
                    logger.info("Empty transcript after barge-in, requesting response replay")
                    self._resume_after_barge_in = False
                    if not self._active_response:
                        await conn.response.create()
                    else:
                        self._pending_response_request = True

        elif event.type == ServerEventType.ERROR:
            if "no active response" not in event.error.message.lower():
                logger.error(f"Error: {event.error.message}")

    # ================================================================
    # AGENT PROCESSING
    # ================================================================

    async def _process_with_agent(self, query_id: str, transcript: str):
        """Sends transcript to the local agent and stores the response."""
        try:
            async with self._processing_lock:
                logger.info(f"[{query_id}] Sending to agent: {transcript}")

                response = await self.agent_bridge.process_message(transcript)

                logger.info(f"[{query_id}] Agent response: {response[:200]}")

                self.pending_queries[query_id].result = response
                self.pending_queries[query_id].state = QueryState.COMPLETED

        except Exception:
            logger.exception(f"[{query_id}] Agent processing failed")
            self.pending_queries[query_id].result = (
                "Entschuldigung, es ist ein Fehler aufgetreten."
            )
            self.pending_queries[query_id].state = QueryState.COMPLETED

    # ================================================================
    # BACKGROUND RESULT CHECKER & INTERRUPT
    # ================================================================

    async def _check_for_completed_queries(self):
        """Polls for completed agent responses and injects them into VoiceLive."""
        while True:
            await asyncio.sleep(0.5)

            for query_id, pending in list(self.pending_queries.items()):
                if pending.state == QueryState.COMPLETED:
                    logger.info(f"[{query_id}] Agent result ready - interrupting")
                    print(f"\n[AGENT RESPONSE READY]")

                    # 1. Stop audio
                    self.audio_processor.skip_pending_audio()

                    # 2. Cancel active response
                    if self._active_response and not self._response_api_done:
                        try:
                            await self.connection.response.cancel()
                        except Exception:
                            pass

                    # 3. Inject agent response
                    await self._inject_agent_response(pending.result)

                    # 4. Clean up
                    pending.state = QueryState.INJECTED
                    del self.pending_queries[query_id]

    async def _inject_agent_response(self, response_text: str):
        """Injects the agent's response and asks VoiceLive to read it aloud.

        We avoid pre-generated assistant messages by sending a prefixed user
        message and instructing VoiceLive to repeat it verbatim.
        """
        # Override instructions for this response only via the message prefix.
        voice_text = f"{self._agent_response_prefix} {response_text}"

        await self.connection.conversation.item.create(
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": voice_text}],
            }
        )

        if self._active_response:
            self._pending_response_request = True
            logger.info("Response already active; will request response after completion")
        else:
            await self.connection.response.create()
            logger.info("Agent response injected, generating audio")


# ============================================================
# MAIN
# ============================================================


def main():
    # Voice Live config
    voicelive_endpoint = os.environ.get("AZURE_VOICELIVE_ENDPOINT")
    voicelive_api_key = os.environ.get("AZURE_VOICELIVE_API_KEY")
    voicelive_model = os.environ.get("AZURE_VOICELIVE_MODEL", "gpt-realtime")
    voice = os.environ.get("AZURE_VOICELIVE_VOICE", "de-DE-ConradNeural")
    use_token_credential = (
        os.environ.get("USE_TOKEN_CREDENTIAL", "false").lower() == "true"
    )

    # Local Agent Framework config
    project_endpoint = (
        os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_EXISTING_AIPROJECT_ENDPOINT")
    )
    model_deployment = (
        os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        or os.environ.get("MODEL_DEPLOYMENT_NAME")
        or os.environ.get("AZURE_AGENT_MODEL")
    )

    # Validate Voice Live config
    if not voicelive_endpoint:
        print("ERROR: AZURE_VOICELIVE_ENDPOINT is not set.")
        print("Copy .env.example to .env in the voiceAgentAgentic/ folder and fill in your values.")
        sys.exit(1)

    if not voicelive_api_key and not use_token_credential:
        print("ERROR: Set AZURE_VOICELIVE_API_KEY or USE_TOKEN_CREDENTIAL=true in .env")
        sys.exit(1)

    # Validate local Agent Framework config
    if not project_endpoint:
        print("ERROR: AZURE_AI_PROJECT_ENDPOINT is not set.")
        print("Set AZURE_AI_PROJECT_ENDPOINT or AZURE_EXISTING_AIPROJECT_ENDPOINT.")
        sys.exit(1)

    if not model_deployment:
        print("ERROR: AZURE_AI_MODEL_DEPLOYMENT_NAME is not set.")
        print("Set AZURE_AI_MODEL_DEPLOYMENT_NAME or MODEL_DEPLOYMENT_NAME.")
        sys.exit(1)

    voicelive_credential = (
        AzureCliCredential()
        if use_token_credential
        else AzureKeyCredential(voicelive_api_key)
    )

    logger.info(
        "Config: endpoint=%s model=%s voice=%s token_auth=%s api_key_set=%s",
        voicelive_endpoint,
        voicelive_model,
        voice,
        use_token_credential,
        bool(voicelive_api_key),
    )
    logger.info(
        "Local agent config: project_endpoint=%s model=%s",
        project_endpoint,
        model_deployment,
    )

    assistant = AgentVoiceAssistant(
        voicelive_endpoint=voicelive_endpoint,
        voicelive_credential=voicelive_credential,
        voicelive_model=voicelive_model,
        voice=voice,
        project_endpoint=project_endpoint,
        model_deployment=model_deployment,
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
    main()
