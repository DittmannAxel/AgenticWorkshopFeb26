# -------------------------------------------------------------------------
# Step 1: Voice Live API with Local Tool Execution
# -------------------------------------------------------------------------
#
# In this example, VoiceLive's built-in realtime model decides WHICH tools
# to call. Your code executes the tools locally as Python functions, then
# feeds the results back into VoiceLive.
#
# Flow:
#   User speaks -> VoiceLive (STT + LLM) -> Tool call event
#     -> Your code runs the tool locally
#     -> Result sent back to VoiceLive -> VoiceLive (TTS) -> User hears response
#
# The async background pattern keeps the conversation natural:
#   1. Immediately acknowledge ("I'm looking that up...")
#   2. Run the tool in the background
#   3. When done, interrupt and deliver the result
# -------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import base64
import inspect
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
from typing import Annotated, Any, Dict, Optional, Union, get_args, get_origin, get_type_hints

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureStandardVoice,
    InputAudioFormat,
    ItemType,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
    FunctionTool,
    FunctionCallOutputItem,
    ToolChoiceLiteral,
    AudioInputTranscriptionOptions,
    Tool,
)
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
logging.basicConfig(
    filename=f"logs/{timestamp}_local_tools.log",
    filemode="w",
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Convert Python tool functions to VoiceLive FunctionTool
# ============================================================


def python_func_to_voicelive_tool(func) -> FunctionTool:
    """Convert a Pydantic-annotated Python function to a VoiceLive FunctionTool.

    Reads type hints like ``Annotated[str, Field(description="...")]`` and
    builds the JSON schema that VoiceLive expects.
    """
    hints = get_type_hints(func, include_extras=True)
    sig = inspect.signature(func)

    properties: Dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        hint = hints.get(param_name)
        param_schema: Dict[str, Any] = {"type": "string"}

        # Extract description from Annotated[str, Field(description=...)]
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            for metadata in args[1:]:
                if hasattr(metadata, "description") and metadata.description:
                    param_schema["description"] = metadata.description
                    break

        properties[param_name] = param_schema

        # If no default value, the parameter is required
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema = {
        "type": "object",
        "properties": properties,
        "required": required,
    }

    return FunctionTool(
        name=func.__name__,
        description=(func.__doc__ or "").strip().split("\n")[0],
        parameters=schema,
    )


# Build dispatch map: function name -> callable
TOOL_DISPATCH: Dict[str, Any] = {func.__name__: func for func in ALL_TOOLS}


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
    """Tracks a running background query."""

    query_id: str
    function_name: str
    call_id: str
    previous_item_id: str
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
# LOCAL TOOL VOICE ASSISTANT
# ============================================================


class LocalToolVoiceAssistant:
    """
    Voice assistant where VoiceLive's model decides tool calls and
    your code executes them locally.

    The async background pattern:
      1. User asks a question
      2. VoiceLive triggers a tool call
      3. IMMEDIATELY: acknowledge ("I'm looking that up...")
      4. BACKGROUND: execute the real tool function
      5. VoiceLive continues with smalltalk
      6. WHEN DONE: interrupt audio, inject result
    """

    def __init__(
        self,
        endpoint: str,
        credential: Union[AzureKeyCredential, AsyncTokenCredential],
        model: str,
        voice: str,
    ):
        self.endpoint = endpoint
        self.credential = credential
        self.model = model
        self.voice = voice

        self.instructions = """Du bist ein professioneller Kundenservice-Agent fuer ein deutsches Unternehmen.

VERHALTEN:
- Antworte immer hoeflich und professionell auf Deutsch (Sie-Form).
- Wenn du ein Tool aufrufst, bekommst du SOFORT eine Bestaetigung dass die Abfrage laeuft.
- Fuehre dann ein kurzes, natuerliches Gespraech mit dem Kunden.
- Sage z.B. "Einen Moment bitte, ich schaue das fuer Sie nach."
- Das Ergebnis wird dir automatisch mitgeteilt, sobald es verfuegbar ist.

FAEHIGKEITEN (ueber Tools):
- Kunden identifizieren (CRM)
- Bestellstatus und -historie abfragen
- Termine pruefen und buchen
- Support-Tickets erstellen

Halte Antworten kurz und klar -- sie werden als Sprache vorgelesen."""

        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.session_ready = False
        self._active_response = False
        self._response_api_done = False

        self._pending_function_call: Optional[Dict[str, Any]] = None
        self.pending_queries: Dict[str, PendingQuery] = {}
        self._result_checker_task: Optional[asyncio.Task] = None

    async def start(self):
        """Starts the Voice Assistant."""
        try:
            logger.info("Connecting to VoiceLive API...")

            async with connect(
                endpoint=self.endpoint,
                credential=self.credential,
                model=self.model,
            ) as connection:
                self.connection = connection
                self.audio_processor = AudioProcessor(connection)

                await self._setup_session()
                self.audio_processor.start_playback()

                self._result_checker_task = asyncio.create_task(
                    self._check_for_completed_queries()
                )

                print("\n" + "=" * 60)
                print("STEP 1: VOICE LIVE + LOCAL TOOLS")
                print(f"Tools registered: {len(ALL_TOOLS)}")
                for t in ALL_TOOLS:
                    print(f"  - {t.__name__}")
                print("Speak into your microphone. Press Ctrl+C to exit.")
                print("=" * 60 + "\n")

                await self._process_events()

        finally:
            if self._result_checker_task:
                self._result_checker_task.cancel()
            if self.audio_processor:
                self.audio_processor.shutdown()

    async def _setup_session(self):
        """Configures the VoiceLive session with tools from src/tools/."""

        voice_config = (
            AzureStandardVoice(name=self.voice) if "-" in self.voice else self.voice
        )

        # Convert all Python tool functions to VoiceLive FunctionTool schemas
        tools: list[Tool] = [python_func_to_voicelive_tool(func) for func in ALL_TOOLS]

        logger.info(
            "Registering %d tools on VoiceLive session: %s",
            len(tools),
            [t.name for t in tools],
        )

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
            tools=tools,
            tool_choice=ToolChoiceLiteral.AUTO,
            input_audio_transcription=AudioInputTranscriptionOptions(model="whisper-1"),
        )

        await self.connection.session.update(session=session_config)
        logger.info("Session configured with local tools")

    async def _process_events(self):
        """Processes events from VoiceLive."""
        async for event in self.connection:
            await self._handle_event(event)

    async def _handle_event(self, event):
        """Event handler with function call support."""
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

            if (
                self._pending_function_call
                and "arguments" in self._pending_function_call
            ):
                await self._handle_function_call(self._pending_function_call)
                self._pending_function_call = None

        elif event.type == ServerEventType.CONVERSATION_ITEM_CREATED:
            if event.item.type == ItemType.FUNCTION_CALL:
                self._pending_function_call = {
                    "name": event.item.name,
                    "call_id": event.item.call_id,
                    "previous_item_id": event.item.id,
                }
                print(f"[Tool call: {event.item.name}]")

        elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            if (
                self._pending_function_call
                and event.call_id == self._pending_function_call["call_id"]
            ):
                self._pending_function_call["arguments"] = event.arguments

        elif event.type == ServerEventType.ERROR:
            if "no active response" not in event.error.message.lower():
                logger.error(f"Error: {event.error.message}")

    # ================================================================
    # FUNCTION CALL HANDLING
    # ================================================================

    async def _handle_function_call(self, function_call_info: Dict[str, Any]):
        """
        1. Send immediate acknowledgement to VoiceLive
        2. Start background task to execute the real tool
        """
        function_name = function_call_info["name"]
        call_id = function_call_info["call_id"]
        previous_item_id = function_call_info["previous_item_id"]
        arguments = function_call_info["arguments"]

        logger.info(f"Function call: {function_name}({arguments})")

        if function_name not in TOOL_DISPATCH:
            logger.warning(f"Unknown tool: {function_name}")
            return

        print(f"[Executing tool in background: {function_name}]")

        # 1. Immediate acknowledgement
        immediate_response = {
            "status": "searching",
            "message": "Abfrage gestartet. Ergebnis folgt in Kuerze.",
        }

        function_output = FunctionCallOutputItem(
            call_id=call_id, output=json.dumps(immediate_response)
        )

        await self.connection.conversation.item.create(
            previous_item_id=previous_item_id, item=function_output
        )

        # VoiceLive will respond with smalltalk
        await self.connection.response.create()

        # 2. Start background tool execution
        query_id = f"{function_name}_{call_id}"

        pending = PendingQuery(
            query_id=query_id,
            function_name=function_name,
            call_id=call_id,
            previous_item_id=previous_item_id,
            state=QueryState.RUNNING,
        )

        pending.task = asyncio.create_task(
            self._execute_tool_in_background(query_id, function_name, arguments)
        )

        self.pending_queries[query_id] = pending

    async def _execute_tool_in_background(
        self, query_id: str, function_name: str, arguments: str
    ):
        """Executes a tool function from src/tools/ in the background."""
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            tool_func = TOOL_DISPATCH[function_name]

            logger.info(f"[{query_id}] Calling {function_name}({args})")

            # Run the tool (use to_thread in case it does blocking I/O)
            result = await asyncio.to_thread(tool_func, **args)

            result_text = json.dumps(result, ensure_ascii=False, default=str)

            self.pending_queries[query_id].result = result_text
            self.pending_queries[query_id].state = QueryState.COMPLETED

            logger.info(f"[{query_id}] Tool completed: {result_text[:200]}")

        except Exception:
            logger.exception(f"[{query_id}] Tool execution failed")
            self.pending_queries[query_id].result = json.dumps(
                {"error": "Tool execution failed. Please try again."}
            )
            self.pending_queries[query_id].state = QueryState.COMPLETED

    # ================================================================
    # BACKGROUND RESULT CHECKER & INTERRUPT
    # ================================================================

    async def _check_for_completed_queries(self):
        """Polls for completed background queries and injects results."""
        while True:
            await asyncio.sleep(0.5)

            for query_id, pending in list(self.pending_queries.items()):
                if pending.state == QueryState.COMPLETED:
                    logger.info(f"[{query_id}] Result ready - interrupting")
                    print(f"\n[RESULT READY: {pending.function_name}]")

                    # 1. Stop audio
                    self.audio_processor.skip_pending_audio()

                    # 2. Cancel active response
                    if self._active_response and not self._response_api_done:
                        try:
                            await self.connection.response.cancel()
                        except Exception:
                            pass

                    # 3. Inject result
                    await self._inject_result(pending.result)

                    # 4. Clean up
                    pending.state = QueryState.INJECTED
                    del self.pending_queries[query_id]

    async def _inject_result(self, result_text: str):
        """Injects the tool result as an assistant message for TTS."""
        full_message = (
            f"Ich habe jetzt das Ergebnis. "
            f"Hier sind die Informationen: {result_text}"
        )

        await self.connection.conversation.item.create(
            item={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": full_message}],
            }
        )

        await self.connection.response.create()
        logger.info("Result injected, generating audio")


# ============================================================
# MAIN
# ============================================================


def main():
    endpoint = os.environ.get("AZURE_VOICELIVE_ENDPOINT")
    api_key = os.environ.get("AZURE_VOICELIVE_API_KEY")
    model = os.environ.get("AZURE_VOICELIVE_MODEL", "gpt-realtime")
    voice = os.environ.get("AZURE_VOICELIVE_VOICE", "de-DE-ConradNeural")
    use_token_credential = (
        os.environ.get("USE_TOKEN_CREDENTIAL", "false").lower() == "true"
    )

    if not endpoint:
        print("ERROR: AZURE_VOICELIVE_ENDPOINT is not set.")
        print("Copy .env.example to .env in the voiceAgentAgentic/ folder and fill in your values.")
        sys.exit(1)

    if not api_key and not use_token_credential:
        print("ERROR: Set AZURE_VOICELIVE_API_KEY or USE_TOKEN_CREDENTIAL=true in .env")
        sys.exit(1)

    credential = (
        AzureCliCredential()
        if use_token_credential
        else AzureKeyCredential(api_key)
    )

    assistant = LocalToolVoiceAssistant(
        endpoint=endpoint,
        credential=credential,
        model=model,
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
    main()
