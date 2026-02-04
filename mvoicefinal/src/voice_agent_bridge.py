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
        agent: Any,  # LangGraph CompiledGraph
        config: Optional[BridgeConfig] = None,
        thread_id: Optional[str] = None,
    ):
        self.voice_service = voice_service
        self.agent = agent
        self.config = config or BridgeConfig()
        self.thread_id = thread_id or str(uuid.uuid4())
        
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
        
        logger.info("VoiceAgentBridge started")
    
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
        
        # Only process user transcripts
        if event.type != VoiceEventType.TRANSCRIPT:
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
            # Remove oldest entries (convert to list, slice, convert back)
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
