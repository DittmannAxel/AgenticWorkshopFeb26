# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# -------------------------------------------------------------------------
"""
Voice-Agent Bridge - Non-blocking integration between VoiceService and a backend agent.

This module provides the core bridge that enables real-time voice conversations
to continue while LangGraph agent processes tool calls in the background.
Results are injected back into the voice session when ready.

Architecture:
    VoiceService (real-time) ←→ VoiceAgentBridge ←→ Agent/Backend (background)
    
    1. Voice transcribes user speech
    2. Bridge classifies query (simple vs data lookup)
    3. Simple queries → VoiceLive handles directly
    4. Data lookups → Spawn background agent task
    5. Agent completes → Inject context back to voice
    6. Voice continues with the new information
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Any

from azure.ai.voicelive.models import FunctionTool, Tool, ToolChoiceLiteral

from src.voice_service import VoiceService, VoiceEvent, VoiceEventType
from src.query_classifier import (
    QueryClassifier,
    KeywordClassifier,
    ClassifierConfig,
    QueryType,
    ClassificationResult,
)
from src.pending_task_manager import (
    PendingTaskManager,
    TaskManagerConfig,
    TaskResult,
    TaskStatus,
)
from src.set_logging import logger
from src.order_agent import OrderAgent, OrderAgentActionType, OrderLookupRequest
from src.order_backend import OrderBackend


# ---------------------------------------------------------------------------
# Function-calling tool definitions for the order-status use case
# ---------------------------------------------------------------------------

ORDER_TOOLS: list[Tool] = [
    FunctionTool(
        name="get_order_status",
        description="Bestellstatus anhand der Bestellnummer abfragen (z.B. ORD-7001)",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Bestellnummer, z.B. ORD-7001"}
            },
            "required": ["order_id"],
        },
    ),
    FunctionTool(
        name="find_orders_by_customer_name",
        description="Bestellungen eines Kunden anhand des Namens suchen",
        parameters={
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Vor- und Nachname des Kunden"}
            },
            "required": ["customer_name"],
        },
    ),
    FunctionTool(
        name="list_all_orders",
        description="Alle Bestellungen auflisten",
        parameters={"type": "object", "properties": {}},
    ),
]

ORDER_TOOL_CHOICE = ToolChoiceLiteral.AUTO


@dataclass
class BridgeConfig:
    """Configuration for the voice-agent bridge."""
    
    # Task management
    max_concurrent_queries: int = 3
    agent_timeout: float = 30.0  # seconds
    
    # Classifier configuration
    classifier_config: Optional[ClassifierConfig] = None
    
    # Acknowledgment phrases (rotated for natural feel)
    acknowledgment_phrases: list[str] = field(default_factory=lambda: [
        "Let me look that up for you.",
        "One moment while I find that information.",
        "I'm checking our records now.",
        "Give me just a second to retrieve that data.",
        "Looking into that for you now.",
    ])
    
    # Context injection templates
    context_template: str = """
The user previously asked: "{query}"

Here is the information I found:
{response}

INSTRUCTIONS FOR RESPONDING:
- Share this information naturally and conversationally
- Summarize the key points - don't read everything verbatim
- If the data is a list, mention how many items and highlight the most relevant ones
- If the user has moved on to a different topic, briefly mention you found the earlier information
- Keep your response concise for voice (avoid long lists)
- Ask if they need any clarification or additional details
"""

    timeout_message: str = """
I'm still working on finding that information about "{query}".
This is taking a bit longer than expected. I'll let you know as soon as I have it.
In the meantime, is there anything else I can help you with?
"""

    error_message: str = """
I encountered an issue looking up that information about "{query}".
Could you try asking in a different way, or let me know if there's something else I can help with?
"""

    # Optional: called right before we speak an injected result (lets the UI stop playback).
    interrupt_playback: Optional[Callable[[], Awaitable[None]]] = None


# Callback types for external handlers
AgentStartCallback = Callable[[str], Awaitable[None]]  # query
AgentCompleteCallback = Callable[[str, str], Awaitable[None]]  # query, response
AgentErrorCallback = Callable[[str, Exception], Awaitable[None]]  # query, error


class VoiceAgentBridge:
    """
    Non-blocking bridge between VoiceService and LangGraph agent.
    
    This bridge enables voice conversations to continue naturally while
    agent tasks process data lookups in the background. Key features:
    
    - Query classification to route appropriately
    - Non-blocking background agent execution
    - Automatic context injection when results are ready
    - Concurrent query management with limits
    - Timeout and error handling
    - Customizable acknowledgment and response templates
    
    Usage:
        # Create components
        voice_service = VoiceService(credential, config)
        agent = await get_agent()
        
        # Create bridge
        bridge = VoiceAgentBridge(voice_service, agent)
        
        # Optional: register callbacks for UI updates
        bridge.on_agent_start(my_start_handler)
        bridge.on_agent_complete(my_complete_handler)
        
        # Start the bridge (registers voice event listener)
        await bridge.start()
        
        # ... voice conversation happens ...
        # Background agent queries run automatically
        
        # Stop when done
        await bridge.stop()
    """
    
    def __init__(
        self,
        voice_service: VoiceService,
        agent: Any,  # LangGraph CompiledGraph or OrderAgent
        config: Optional[BridgeConfig] = None,
        thread_id: Optional[str] = None,
        order_backend: Optional[OrderBackend] = None,
    ):
        self.voice_service = voice_service
        self.agent = agent
        self.config = config or BridgeConfig()
        self.thread_id = thread_id or str(uuid.uuid4())
        self._order_backend = order_backend

        # Initialize classifier
        self.classifier: QueryClassifier = KeywordClassifier(
            self.config.classifier_config
        )

        # Initialize task manager
        task_config = TaskManagerConfig(
            max_concurrent_tasks=self.config.max_concurrent_queries,
            default_timeout=self.config.agent_timeout,
        )
        self.task_manager = PendingTaskManager(task_config)

        # State
        self._running = False
        self._ack_index = 0

        # External callbacks
        self._on_agent_start: Optional[AgentStartCallback] = None
        self._on_agent_complete: Optional[AgentCompleteCallback] = None
        self._on_agent_error: Optional[AgentErrorCallback] = None

        # Track processed transcripts to avoid duplicates
        self._processed_transcripts: set[str] = set()
        self._max_processed_cache = 100

        # Pending function call context (set by FUNCTION_CALL_STARTED,
        # consumed by FUNCTION_CALL_ARGUMENTS_DONE)
        self._pending_function_call: Optional[dict[str, Any]] = None
    
    @property
    def pending_query_count(self) -> int:
        """Number of queries currently being processed."""
        return self.task_manager.pending_count
    
    @property
    def pending_queries(self) -> list[str]:
        """List of query texts currently being processed."""
        return self.task_manager.get_pending_queries()
    
    @property
    def is_running(self) -> bool:
        """Check if bridge is active."""
        return self._running
    
    def on_agent_start(self, callback: AgentStartCallback) -> None:
        """Register callback for when agent starts processing."""
        self._on_agent_start = callback
    
    def on_agent_complete(self, callback: AgentCompleteCallback) -> None:
        """Register callback for when agent completes successfully."""
        self._on_agent_complete = callback
    
    def on_agent_error(self, callback: AgentErrorCallback) -> None:
        """Register callback for when agent encounters an error."""
        self._on_agent_error = callback
    
    async def start(self) -> None:
        """Start the bridge and register as voice event listener."""
        if self._running:
            logger.warning("VoiceAgentBridge is already running")
            return
        
        self._running = True
        
        # Start task manager
        await self.task_manager.start()
        
        # Register internal task callbacks
        self.task_manager.on_task_complete(self._on_task_complete)
        self.task_manager.on_task_error(self._on_task_error)
        
        # Register as voice event listener
        self.voice_service.on_event(self._handle_voice_event)

        mode = "native function calling" if self._order_backend else "transcript interception"
        logger.info("VoiceAgentBridge started (mode: %s)", mode)
        print(f"[Bridge] Started in mode: {mode}")
    
    async def stop(self) -> None:
        """Stop the bridge and cleanup resources."""
        if not self._running:
            return
        
        self._running = False
        
        # Unregister voice event listener
        self.voice_service.remove_event_handler(self._handle_voice_event)
        
        # Stop task manager (cancels pending tasks)
        await self.task_manager.shutdown()
        
        # Clear processed cache
        self._processed_transcripts.clear()
        
        logger.info("VoiceAgentBridge stopped")
    
    def classify_query(self, text: str) -> ClassificationResult:
        """
        Classify a query to determine routing.
        
        Can be overridden for custom classification logic.
        """
        return self.classifier.classify(text)
    
    async def _handle_voice_event(self, event: VoiceEvent) -> None:
        """Handle incoming voice events."""
        if not self._running:
            return

        # --- Function-calling path (native VoiceLive tools) ----------------
        if event.type == VoiceEventType.FUNCTION_CALL_STARTED:
            self._pending_function_call = {
                "name": event.data.get("name"),
                "call_id": event.data.get("call_id"),
                "item_id": event.data.get("item_id"),
            }
            logger.info("Function call started: %s", self._pending_function_call.get("name"))
            return

        if event.type == VoiceEventType.FUNCTION_CALL_ARGUMENTS_DONE:
            fc = self._pending_function_call or {}
            call_id = event.data.get("call_id") or fc.get("call_id")
            name = event.data.get("name") or fc.get("name")
            item_id = fc.get("item_id")
            arguments = event.data.get("arguments", "{}")
            self._pending_function_call = None

            if name and call_id and self._order_backend:
                # MUST run as a background task — awaiting here would block
                # the event loop and prevent RESPONSE_DONE from being
                # processed, deadlocking the wait for _active_response.
                asyncio.create_task(self._handle_function_call(
                    name=name,
                    call_id=call_id,
                    arguments=arguments,
                    previous_item_id=item_id,
                ))
            return

        # --- Transcript-interception path (fallback when no tools) ---------
        if event.type != VoiceEventType.TRANSCRIPT:
            return

        # When function calling is active, the model decides tool use on its
        # own — we do NOT intercept transcripts for OrderAgent routing.
        if self._order_backend:
            logger.debug("Skipping transcript interception (function-calling mode active)")
            return

        role = event.data.get("role", "")
        transcript = event.data.get("transcript", "")

        if role != "user" or not transcript:
            return

        # Deduplicate (VoiceLive may emit same transcript multiple times)
        transcript_key = transcript.strip().lower()[:100]
        if transcript_key in self._processed_transcripts:
            return

        self._processed_transcripts.add(transcript_key)

        # Trim cache if too large
        if len(self._processed_transcripts) > self._max_processed_cache:
            excess = len(self._processed_transcripts) - self._max_processed_cache // 2
            items = list(self._processed_transcripts)
            self._processed_transcripts = set(items[excess:])

        # Process the transcript
        await self._process_user_transcript(transcript)
    
    async def _process_user_transcript(self, text: str) -> None:
        """Process a user transcript and potentially spawn agent task."""
        # If we're using the order agent, let it decide first. This ensures that
        # order-related utterances don't accidentally bypass the lookup flow and
        # fall back to generic model responses (which can hallucinate).
        if not isinstance(self.agent, OrderAgent):
            # Classify the query (generic routing)
            result = self.classify_query(text)
            logger.info(
                f"Query classified as {result.query_type.value} "
                f"(confidence: {result.confidence:.2f}): {text[:50]}..."
            )

            if result.query_type != QueryType.DATA_LOOKUP:
                await self.voice_service.request_response()
                return

            await self.voice_service.add_system_message(
                'Hinweis: Für diese Demo ist kein generischer Agent konfiguriert. '
                'Bitte stellen Sie eine Bestellfrage oder konfigurieren Sie einen Backend-Agent.'
            )
            await self.voice_service.request_response()
            return

        # OrderAgent path
        action = await self.agent.decide(text)

        if action.type == OrderAgentActionType.PASS_THROUGH:
            # Server-side VAD already auto-generates a response for general
            # chat, so no explicit request_response() call is needed here.
            return

        if action.type == OrderAgentActionType.ASK_IDENTIFIER and action.say:
            await self.voice_service.add_system_message(
                f'User said: "{text}"\n\n'
                "Aufgabe: Stellen Sie dem Kunden genau diese Frage. "
                "Sagen Sie ausschließlich diesen Text, nichts hinzufügen:\n"
                f"{action.say}"
            )
            await self.voice_service.request_response(interrupt=True)
            return

        if action.type in (OrderAgentActionType.LOOKUP, OrderAgentActionType.LIST_ORDERS) and action.lookup:
            # Immediate acknowledgement to keep the voice conversation snappy.
            if action.say:
                await self.voice_service.add_system_message(
                    f'User said: "{text}"\n\n'
                    "Aufgabe: Sagen Sie dem Kunden genau diesen Satz. "
                    "Sagen Sie ausschließlich diesen Text, nichts hinzufügen:\n"
                    f"{action.say}"
                )
                await self.voice_service.request_response(interrupt=True)

        # Check capacity
        if not self.task_manager.can_accept_task:
            logger.warning(
                f"Cannot spawn agent task: at capacity "
                f"({self.pending_query_count}/{self.config.max_concurrent_queries})"
            )
            # Speak a concise “busy” message if we cannot lookup right now.
            await self.voice_service.add_system_message(
                f'User said: "{text}"\n\nAufgabe: Sagen Sie dem Kunden kurz: '
                "Ich bin gerade mit einer anderen Abfrage beschäftigt. Bitte versuchen Sie es gleich noch einmal."
            )
            await self.voice_service.request_response()
            return
        
        # Spawn background lookup task
        await self._spawn_order_lookup_task(original_query=text, request=action.lookup)
        return
    
    # ------------------------------------------------------------------
    # Native function-calling path
    # ------------------------------------------------------------------

    async def _handle_function_call(
        self,
        name: str,
        call_id: str,
        arguments: str,
        previous_item_id: Optional[str] = None,
    ) -> None:
        """Handle a function call from VoiceLive (native tool use).

        This runs as a background task (``asyncio.create_task``) so it does
        NOT block the event-processing loop.  That is critical — the wait
        for ``_active_response`` to clear requires ``RESPONSE_DONE`` to be
        processed by the event loop.
        """
        try:
            await self._do_function_call(
                name=name,
                call_id=call_id,
                arguments=arguments,
                previous_item_id=previous_item_id,
            )
        except Exception as exc:
            logger.exception("Function-call task crashed")
            print(f"[Bridge] FATAL: function-call task crashed: {exc!r}")

    async def _do_function_call(
        self,
        name: str,
        call_id: str,
        arguments: str,
        previous_item_id: Optional[str] = None,
    ) -> None:
        """Full function-call lifecycle: wait → ack → lookup → inject result."""
        assert self._order_backend is not None
        logger.info("Function call: %s(%s)", name, arguments)
        print(f"[Bridge] Handling function call: {name}({arguments})")

        # Notify external callback
        if self._on_agent_start:
            try:
                await self._on_agent_start(f"{name}({arguments})")
            except Exception as e:
                logger.error("Error in agent start callback: %s", e)

        # ---- 0. Wait for the response that produced this function call ----
        # FUNCTION_CALL_ARGUMENTS_DONE fires before RESPONSE_DONE, so the
        # server still considers a response active.  We must wait.
        if self.voice_service._active_response:
            print("[Bridge] Step 0: Waiting for function-call response to finish...")
            for i in range(50):  # 50 × 100 ms = 5 s
                if not self.voice_service._active_response:
                    print(f"[Bridge] Step 0: Response finished after {(i+1)*100}ms")
                    break
                await asyncio.sleep(0.1)
            else:
                print("[Bridge] Step 0: WARNING — timed out, forcing _active_response=False")
                self.voice_service._active_response = False

        # ---- 1. Send immediate ack so the model speaks a filler ----
        ack = json.dumps({"status": "searching", "message": "Abfrage gestartet, Ergebnis folgt."})
        print("[Bridge] Step 1: Sending immediate ack -> model will speak filler")
        await self.voice_service.send_function_call_output(
            call_id=call_id, output=ack, previous_item_id=previous_item_id,
        )
        await self.voice_service.request_response()

        # ---- 2. Execute the real backend lookup ----
        print(f"[Bridge] Step 2: Backend lookup: {name}({arguments})")
        try:
            args = json.loads(arguments) if arguments else {}
            result: Any

            if name == "get_order_status":
                order_id = args.get("order_id", "")
                print(f"[Bridge] -> get_order_status(order_id={order_id!r})")
                result = await self._order_backend.get_order_status(order_id)
            elif name == "find_orders_by_customer_name":
                customer_name = args.get("customer_name", "")
                print(f"[Bridge] -> find_orders_by_customer_name(customer_name={customer_name!r})")
                orders = await self._order_backend.find_recent_orders_by_customer_name(customer_name)
                result = {"found": bool(orders), "orders": orders, "customer_name": customer_name}
            elif name == "list_all_orders":
                print("[Bridge] -> list_orders()")
                orders = await self._order_backend.list_orders()
                result = {"found": bool(orders), "orders": orders}
            else:
                print(f"[Bridge] -> Unknown function: {name}")
                result = {"error": f"Unknown function: {name}"}

            result_text = json.dumps(result, ensure_ascii=False, default=str)
            logger.info("Function call %s completed: %s", name, result_text[:200])
            print(f"[Bridge] Backend result ({len(result_text)} chars): {result_text[:150]}...")

        except Exception as exc:
            logger.exception("Function call %s failed", name)
            result_text = json.dumps({"error": "Backend lookup failed. Please try again."})
            print(f"[Bridge] Backend lookup FAILED for {name}: {exc!r}")

        # ---- 3. Interrupt filler and cancel active response ----
        print("[Bridge] Step 3: Interrupting filler, cancelling active response")
        if self.config.interrupt_playback:
            try:
                await self.config.interrupt_playback()
            except Exception:
                logger.exception("interrupt_playback callback failed")

        await self.voice_service.cancel_response(wait=True)

        # ---- 4. Send the real result ----
        print("[Bridge] Step 4: Sending real FunctionCallOutputItem")
        await self.voice_service.send_function_call_output(
            call_id=call_id, output=result_text, previous_item_id=previous_item_id,
        )

        # ---- 5. Ask model to speak the result ----
        print("[Bridge] Step 5: Requesting model to speak real data")
        await self.voice_service.request_response()
        print("[Bridge] Done — response.create() sent")

        # Notify external callback
        if self._on_agent_complete:
            try:
                await self._on_agent_complete(f"{name}({arguments})", result_text)
            except Exception as e:
                logger.error("Error in agent complete callback: %s", e)

    # ------------------------------------------------------------------
    # Legacy transcript-interception path
    # ------------------------------------------------------------------

    async def _spawn_order_lookup_task(self, original_query: str, request: OrderLookupRequest) -> None:
        """Spawn a non-blocking order lookup task for the query."""
        logger.info(f"Spawning order lookup task for: {original_query[:50]}...")
        
        # Notify external callback
        if self._on_agent_start:
            try:
                await self._on_agent_start(original_query)
            except Exception as e:
                logger.error(f"Error in agent start callback: {e}")
        
        # Create the lookup coroutine
        lookup_coro = self.agent.lookup(request)
        
        # Spawn via task manager (handles timeout, tracking)
        task_id = await self.task_manager.spawn(
            query=original_query,
            coroutine=lookup_coro,
            timeout=self.config.agent_timeout,
        )
        
        if task_id:
            logger.info(f"Agent task {task_id} spawned for query")
        else:
            logger.warning("Failed to spawn agent task")
    
    async def _on_task_complete(
        self, 
        task_id: str, 
        query: str, 
        result: TaskResult
    ) -> None:
        """Handle successful agent task completion."""
        if not result.success or not result.data:
            return
        
        response = self._format_order_result(result.data)
        if not response:
            logger.warning(f"No response formatted for task {task_id}")
            return
        
        logger.info(
            f"Agent task {task_id} completed in {result.duration_ms:.0f}ms, "
            f"injecting context"
        )
        
        if self.config.interrupt_playback:
            try:
                await self.config.interrupt_playback()
            except Exception:
                logger.exception("interrupt_playback callback failed")

        # Ask the model to speak the result now (create a new conversation item as trigger).
        context = self._format_context(query, response)
        await self.voice_service.add_system_message(
            "Zusatzkontext:\n"
            f"{context}\n\n"
            "WICHTIG: Verwenden Sie ausschließlich Fakten aus dem Zusatzkontext. "
            "Erfinden Sie keine Produkte, Artikel oder Details. "
            "Wenn eine Information fehlt, sagen Sie: 'Dazu habe ich keine Information.'\n\n"
            "Aufgabe: Erklären Sie dem Kunden kurz die Informationen aus dem Zusatzkontext "
            "und fragen Sie am Ende knapp nach, ob Sie noch weiterhelfen können."
        )
        await self.voice_service.request_response(interrupt=True)
        
        # Notify external callback
        if self._on_agent_complete:
            try:
                await self._on_agent_complete(query, response)
            except Exception as e:
                logger.error(f"Error in agent complete callback: {e}")
    
    async def _on_task_error(
        self, 
        task_id: str, 
        query: str, 
        error: str
    ) -> None:
        """Handle agent task error or timeout."""
        logger.warning(f"Agent task {task_id} failed: {error}")
        
        # Determine error type and inject appropriate message
        if "timeout" in error.lower():
            context = self.config.timeout_message.format(query=query[:50])
        else:
            context = self.config.error_message.format(query=query[:50])
        
        await self.voice_service.add_system_message(
            f"{context}\n\n"
            "WICHTIG: Keine Vermutungen anstellen. Keine Details erfinden.\n\n"
            "Aufgabe: Entschuldigen Sie sich kurz und bitten Sie den Kunden, "
            "die Bestellnummer oder seinen Namen noch einmal zu nennen."
        )
        await self.voice_service.request_response(interrupt=True)
        
        # Notify external callback
        if self._on_agent_error:
            try:
                await self._on_agent_error(query, Exception(error))
            except Exception as e:
                logger.error(f"Error in agent error callback: {e}")
    
    def _format_order_result(self, data: Any) -> Optional[str]:
        """Turn backend JSON into a short, voice-friendly summary string."""
        if not isinstance(data, dict):
            return str(data)

        if data.get("found") is False:
            err = data.get("error") or "Order not found."
            return (
                f"{err} Bitte nennen Sie mir Ihre Bestellnummer (z.B. ORD-<nummer>) "
                "oder Ihren Namen, damit ich es erneut prüfen kann."
            )

        # Order-by-id response: pass the full order payload to the voice model (so it can
        # summarize without hallucinating).
        if "id" in data and "status" in data:
            order_payload = {k: v for k, v in data.items() if k != "found"}
            return "order:\n" + json.dumps(order_payload, ensure_ascii=False, indent=2, sort_keys=True)

        # Orders-by-customer / list-all response.
        orders = data.get("orders")
        if isinstance(orders, list):
            if not orders:
                name = data.get("customer_name") or "Ihrem Namen"
                return f"Ich habe keine Bestellungen zu {name} gefunden. Können Sie mir eine Bestellnummer nennen?"

            payload = {k: v for k, v in data.items() if k != "found"}
            payload["order_count"] = len(orders)
            return "orders:\n" + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

        return None
    
    def _format_context(self, query: str, response: str) -> str:
        """Format agent response for injection into voice session."""
        return self.config.context_template.format(
            query=query,
            response=response
        )
    
    def get_acknowledgment(self) -> str:
        """Get a rotating acknowledgment phrase."""
        phrase = self.config.acknowledgment_phrases[self._ack_index]
        self._ack_index = (self._ack_index + 1) % len(self.config.acknowledgment_phrases)
        return phrase
    
    def add_data_keyword(self, keyword: str) -> None:
        """Add a keyword that triggers agent processing."""
        if isinstance(self.classifier, KeywordClassifier):
            self.classifier.add_keyword(keyword)
    
    def remove_data_keyword(self, keyword: str) -> None:
        """Remove a keyword from agent trigger list."""
        if isinstance(self.classifier, KeywordClassifier):
            self.classifier.remove_keyword(keyword)


class VoiceAgentBridgeBuilder:
    """Fluent builder for VoiceAgentBridge instances."""
    
    def __init__(self):
        self._voice_service: Optional[VoiceService] = None
        self._agent: Any = None
        self._config = BridgeConfig()
    
    def with_voice_service(self, service: VoiceService) -> "VoiceAgentBridgeBuilder":
        """Set the voice service."""
        self._voice_service = service
        return self
    
    def with_agent(self, agent: Any) -> "VoiceAgentBridgeBuilder":
        """Set the LangGraph agent."""
        self._agent = agent
        return self
    
    def with_timeout(self, timeout: float) -> "VoiceAgentBridgeBuilder":
        """Set agent query timeout in seconds."""
        self._config.agent_timeout = timeout
        return self
    
    def with_max_concurrent(self, max_queries: int) -> "VoiceAgentBridgeBuilder":
        """Set maximum concurrent agent queries."""
        self._config.max_concurrent_queries = max_queries
        return self
    
    def with_acknowledgments(self, phrases: list[str]) -> "VoiceAgentBridgeBuilder":
        """Set acknowledgment phrases."""
        self._config.acknowledgment_phrases = phrases
        return self
    
    def with_context_template(self, template: str) -> "VoiceAgentBridgeBuilder":
        """Set context injection template."""
        self._config.context_template = template
        return self
    
    def with_data_keywords(self, keywords: list[str]) -> "VoiceAgentBridgeBuilder":
        """Set data keywords for classifier."""
        if self._config.classifier_config is None:
            self._config.classifier_config = ClassifierConfig()
        self._config.classifier_config.data_keywords = keywords
        return self
    
    def build(self) -> VoiceAgentBridge:
        """Build the VoiceAgentBridge instance."""
        if not self._voice_service:
            raise ValueError("VoiceService is required")
        if not self._agent:
            raise ValueError("Agent is required")
        
        return VoiceAgentBridge(
            voice_service=self._voice_service,
            agent=self._agent,
            config=self._config,
        )
