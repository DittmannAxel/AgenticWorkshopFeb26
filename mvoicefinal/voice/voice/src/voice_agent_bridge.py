# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# -------------------------------------------------------------------------
"""
Voice-Agent Bridge - Non-blocking integration between VoiceService and LangGraph.

This module provides the core bridge that enables real-time voice conversations
to continue while LangGraph agent processes tool calls in the background.
Results are injected back into the voice session when ready.

Architecture:
    VoiceService (real-time) ←→ VoiceAgentBridge ←→ LangGraph Agent (background)
    
    1. Voice transcribes user speech
    2. Bridge classifies query (simple vs data lookup)
    3. Simple queries → VoiceLive handles directly
    4. Data lookups → Spawn background agent task
    5. Agent completes → Inject context back to voice
    6. Voice continues with the new information
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Any

from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.runnables.config import RunnableConfig

from voice.src.voice_service import VoiceService, VoiceEvent, VoiceEventType
from voice.src.query_classifier import (
    QueryClassifier, 
    KeywordClassifier, 
    ClassifierConfig,
    QueryType,
    ClassificationResult,
)
from voice.src.pending_task_manager import (
    PendingTaskManager,
    TaskManagerConfig,
    TaskResult,
    TaskStatus,
)
from voice.src.set_logging import logger


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
        # Classify the query
        result = self.classify_query(text)
        
        logger.info(
            f"Query classified as {result.query_type.value} "
            f"(confidence: {result.confidence:.2f}): {text[:50]}..."
        )
        
        # Only spawn agent for data lookups
        if result.query_type != QueryType.DATA_LOOKUP:
            return
        
        # Check capacity
        if not self.task_manager.can_accept_task:
            logger.warning(
                f"Cannot spawn agent task: at capacity "
                f"({self.pending_query_count}/{self.config.max_concurrent_queries})"
            )
            return
        
        # Spawn background agent task
        await self._spawn_agent_task(text)
    
    async def _spawn_agent_task(self, query: str) -> None:
        """Spawn a non-blocking agent task for the query."""
        logger.info(f"Spawning agent task for: {query[:50]}...")
        
        # Notify external callback
        if self._on_agent_start:
            try:
                await self._on_agent_start(query)
            except Exception as e:
                logger.error(f"Error in agent start callback: {e}")
        
        # Create the agent coroutine
        agent_coro = self._run_agent(query)
        
        # Spawn via task manager (handles timeout, tracking)
        task_id = await self.task_manager.spawn(
            query=query,
            coroutine=agent_coro,
            timeout=self.config.agent_timeout,
        )
        
        if task_id:
            logger.info(f"Agent task {task_id} spawned for query")
        else:
            logger.warning("Failed to spawn agent task")
    
    async def _run_agent(self, query: str) -> dict:
        """Run the LangGraph agent for a query."""
        config = RunnableConfig(
            configurable={"thread_id": self.thread_id}
        )
        return await self.agent.ainvoke(
            {"messages": [HumanMessage(content=query)]},
            config=config,
        )
    
    async def _on_task_complete(
        self, 
        task_id: str, 
        query: str, 
        result: TaskResult
    ) -> None:
        """Handle successful agent task completion."""
        if not result.success or not result.data:
            return
        
        # Extract response from agent result
        response = self._extract_agent_response(result.data)
        
        if not response:
            logger.warning(f"No response extracted from agent result for task {task_id}")
            return
        
        logger.info(
            f"Agent task {task_id} completed in {result.duration_ms:.0f}ms, "
            f"injecting context"
        )
        
        # Format and inject context
        context = self._format_context(query, response)
        await self.voice_service.inject_context(context)
        
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
        
        await self.voice_service.inject_context(context)
        
        # Notify external callback
        if self._on_agent_error:
            try:
                await self._on_agent_error(query, Exception(error))
            except Exception as e:
                logger.error(f"Error in agent error callback: {e}")
    
    def _extract_agent_response(self, agent_result: dict) -> Optional[str]:
        """Extract the text response from agent result."""
        messages = agent_result.get("messages", [])
        
        # Find the last AI message with content
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                # Handle string content
                if isinstance(msg.content, str):
                    return msg.content
                # Handle list content (multimodal)
                elif isinstance(msg.content, list):
                    text_parts = [
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in msg.content
                    ]
                    return " ".join(filter(None, text_parts))
        
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
