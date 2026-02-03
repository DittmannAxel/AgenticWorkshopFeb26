# -------------------------------------------------------------------------
# Step 2: Voice Live API + Azure AI Agent SDK
# -------------------------------------------------------------------------
#
# In this example, VoiceLive handles ONLY the audio (STT + TTS).
# The Azure AI Agent Service handles reasoning and tool execution.
#
# Flow:
#   User speaks -> VoiceLive (STT) -> Transcription event
#     -> Your code sends text to Agent SDK
#     -> Agent SDK calls tools automatically, generates response
#     -> Your code injects response into VoiceLive -> VoiceLive (TTS) -> User
#
# Key difference from Step 1:
#   - NO tools registered on the VoiceLive session
#   - Tools registered on the Agent SDK via ToolSet + FunctionTool
#   - Trigger is TRANSCRIPTION_COMPLETED, not FUNCTION_CALL
#   - Agent SDK has persistent conversation threads (multi-turn memory)
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

# Agent SDK (synchronous -- will be called via asyncio.to_thread)
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import (
    FunctionTool as AgentFunctionTool,
    ToolSet,
    MessageRole,
    ListSortOrder,
)
from azure.identity import DefaultAzureCredential

from dotenv import load_dotenv
import pyaudio

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection

# ---------------------------------------------------------------------------
# Path setup: import real tools from src/tools/
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.tools import ALL_TOOLS  # noqa: E402

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


# ============================================================
# AGENT BRIDGE: Wraps the Azure AI Agent SDK
# ============================================================


class AgentBridge:
    """Bridges the synchronous Azure AI Agent SDK for use in an async context.

    Creates a Foundry Agent with all tools registered. Processes user
    messages synchronously (the SDK's runs.create_and_process is blocking),
    so all calls must go through asyncio.to_thread().
    """

    def __init__(self, endpoint: str, project: str, model: str, tools: list):
        self.agent_endpoint = f"{endpoint}/api/projects/{project}"
        self.model = model
        self.tools = tools
        self.client: Optional[AgentsClient] = None
        self.agent = None
        self.thread_id: Optional[str] = None

    def initialize(self):
        """Create the Agent SDK client, agent, and conversation thread.

        This is synchronous -- call via asyncio.to_thread() from async code.
        """
        self.client = AgentsClient(
            endpoint=self.agent_endpoint,
            credential=DefaultAzureCredential(),
        )

        # Register all tool functions with the SDK
        toolset = ToolSet()
        functions = AgentFunctionTool(self.tools)
        toolset.add(functions)

        # Enable auto function calling: the SDK will execute tool functions
        # automatically when the agent requests them
        self.client.enable_auto_function_calls(toolset)

        # Load system prompt
        system_prompt = load_system_prompt()

        # Create the agent
        self.agent = self.client.create_agent(
            model=self.model,
            name="voice-customer-service-agent",
            instructions=system_prompt,
            toolset=toolset,
        )
        logger.info("Agent created: id=%s, model=%s", self.agent.id, self.model)

        # Create a conversation thread for this session
        thread = self.client.threads.create()
        self.thread_id = thread.id
        logger.info("Thread created: %s", self.thread_id)

    def process_message(self, user_text: str) -> str:
        """Send user text to the agent and return the response.

        This is SYNCHRONOUS -- must be called via asyncio.to_thread().

        The agent will:
        1. Interpret the user's intent
        2. Call tools if needed (auto-executed by the SDK)
        3. Generate a natural language response
        """
        assert self.client is not None and self.agent is not None

        self.client.messages.create(
            thread_id=self.thread_id,
            role=MessageRole.USER,
            content=user_text,
        )

        run = self.client.runs.create_and_process(
            thread_id=self.thread_id,
            agent_id=self.agent.id,
        )
        logger.info("Agent run completed: status=%s", run.status)

        if run.status == "failed":
            logger.error("Agent run failed: %s", run.last_error)
            return "Entschuldigung, es ist ein Fehler aufgetreten. Bitte versuchen Sie es erneut."

        messages = self.client.messages.list(
            thread_id=self.thread_id,
            order=ListSortOrder.DESCENDING,
        )

        for msg in messages:
            if msg.role == MessageRole.AGENT and msg.text_messages:
                response_text = msg.text_messages[-1].text.value
                logger.info("Agent response: %s", response_text[:200])
                return response_text

        return "Entschuldigung, ich konnte Ihre Anfrage nicht verarbeiten."

    def cleanup(self):
        """Delete the agent to free resources."""
        if self.client and self.agent:
            self.client.delete_agent(self.agent.id)
            logger.info("Agent deleted: %s", self.agent.id)


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
    Azure AI Agent SDK handles reasoning and tool execution.

    Key difference from Step 1:
      - VoiceLive session has NO tools registered
      - Transcription events trigger agent calls
      - Agent SDK decides which tools to call and executes them
      - Agent response is injected back for TTS
    """

    def __init__(
        self,
        voicelive_endpoint: str,
        voicelive_credential: Union[AzureKeyCredential, AsyncTokenCredential],
        voicelive_model: str,
        voice: str,
        agent_endpoint: str,
        agent_project: str,
        agent_model: str,
    ):
        self.voicelive_endpoint = voicelive_endpoint
        self.voicelive_credential = voicelive_credential
        self.voicelive_model = voicelive_model
        self.voice = voice

        # Agent bridge handles the Foundry Agent SDK
        self.agent_bridge = AgentBridge(
            endpoint=agent_endpoint,
            project=agent_project,
            model=agent_model,
            tools=ALL_TOOLS,
        )

        # VoiceLive instructions: minimal, just acknowledge and wait
        self.instructions = """Du bist eine Sprachschnittstelle fuer einen Kundenservice.

WICHTIG:
- Wenn der Nutzer etwas sagt, antworte KURZ mit einer Bestaetigung wie
  "Einen Moment bitte, ich schaue das fuer Sie nach."
- Halte dich kurz. Du wirst gleich die eigentliche Antwort erhalten.
- Antworte auf Deutsch in der Sie-Form."""

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
        """Starts the Voice Assistant with Agent SDK."""
        try:
            # Initialize the Agent SDK (synchronous, run in thread)
            print("Initializing Azure AI Agent Service...")
            await asyncio.to_thread(self.agent_bridge.initialize)
            print(f"Agent created with {len(ALL_TOOLS)} tools:")
            for t in ALL_TOOLS:
                print(f"  - {t.__name__}")

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
                print("STEP 2: VOICE LIVE + AGENT SDK")
                print("VoiceLive handles audio. Agent SDK handles reasoning + tools.")
                print("Speak into your microphone. Press Ctrl+C to exit.")
                print("=" * 60 + "\n")

                await self._process_events()

        finally:
            if self._result_checker_task:
                self._result_checker_task.cancel()
            if self.audio_processor:
                self.audio_processor.shutdown()
            # Clean up agent
            try:
                await asyncio.to_thread(self.agent_bridge.cleanup)
            except Exception:
                pass

    async def _setup_session(self):
        """Configures VoiceLive session -- NO tools, just audio + transcription."""

        voice_config = (
            AzureStandardVoice(name=self.voice) if "-" in self.voice else self.voice
        )

        # NOTE: No tools registered here! VoiceLive is audio-only.
        # The Agent SDK handles all tool logic.
        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=self.instructions,
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
            # Transcription is critical: this is how we get the user's text
            # to send to the Agent SDK
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
        # we send the transcript to the Agent SDK.
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
        """Sends transcript to the Agent SDK and stores the response."""
        try:
            async with self._processing_lock:
                logger.info(f"[{query_id}] Sending to agent: {transcript}")

                # The Agent SDK is synchronous, so we run it in a thread
                response = await asyncio.to_thread(
                    self.agent_bridge.process_message, transcript
                )

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
        """Injects the agent's response as an assistant message for TTS."""
        await self.connection.conversation.item.create(
            item={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": response_text}],
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

    # Agent SDK config
    agent_endpoint = os.environ.get("AZURE_AGENT_ENDPOINT")
    agent_project = os.environ.get("AZURE_AGENT_PROJECT")
    agent_model = os.environ.get("AZURE_AGENT_MODEL", "gpt-4.1")

    # Validate Voice Live config
    if not voicelive_endpoint:
        print("ERROR: AZURE_VOICELIVE_ENDPOINT is not set.")
        print("Copy .env.example to .env in the voiceAgentAgentic/ folder and fill in your values.")
        sys.exit(1)

    if not voicelive_api_key and not use_token_credential:
        print("ERROR: Set AZURE_VOICELIVE_API_KEY or USE_TOKEN_CREDENTIAL=true in .env")
        sys.exit(1)

    # Validate Agent SDK config
    if not agent_endpoint:
        print("ERROR: AZURE_AGENT_ENDPOINT is not set.")
        print("This is required for Step 2 (Agent SDK integration).")
        sys.exit(1)

    if not agent_project:
        print("ERROR: AZURE_AGENT_PROJECT is not set.")
        print("Set your AI Foundry project name in .env")
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
        "Agent config: endpoint=%s project=%s model=%s",
        agent_endpoint,
        agent_project,
        agent_model,
    )

    assistant = AgentVoiceAssistant(
        voicelive_endpoint=voicelive_endpoint,
        voicelive_credential=voicelive_credential,
        voicelive_model=voicelive_model,
        voice=voice,
        agent_endpoint=agent_endpoint,
        agent_project=agent_project,
        agent_model=agent_model,
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
