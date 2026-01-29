"""Foundry Agent Service client for AI-powered conversation handling.

Wraps the azure-ai-agents SDK to create and run agents that process
customer intents, execute tools, and generate responses.

Docs:
- Overview: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/overview
- Quickstart: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/quickstart
- Python SDK: https://learn.microsoft.com/en-us/python/api/overview/azure/ai-agents-readme
- Tools: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools-classic/overview
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import (
    FunctionTool,
    ToolSet,
    MessageRole,
    ListSortOrder,
)
from azure.identity import DefaultAzureCredential

from .config import VoiceAgentConfig

logger = logging.getLogger(__name__)

# Load the system prompt from the prompts directory
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SYSTEM_PROMPT_PATH = _PROMPTS_DIR / "system_prompt.md"


class FoundryAgentClient:
    """Client for the Microsoft Foundry Agent Service.

    Creates a conversational agent with tool-calling capabilities.
    The agent processes transcribed speech input and returns text responses
    that are then converted back to speech via Voice Live TTS.

    Usage::

        config = VoiceAgentConfig()
        agent_client = FoundryAgentClient(config, tools=[crm_tool, calendar_tool])
        await agent_client.initialize()

        response = await agent_client.process_message(thread_id, "Wo ist meine Bestellung?")
        print(response)  # "Ihre Bestellung wird morgen zwischen 10-14 Uhr geliefert."
    """

    def __init__(self, config: VoiceAgentConfig, tools: list[Any] | None = None) -> None:
        self._config = config
        self._tool_functions = tools or []
        self._client: AgentsClient | None = None
        self._agent = None

    async def initialize(self) -> None:
        """Create the Foundry Agent with configured tools and instructions.

        This sets up:
        1. The AgentsClient connection to Azure AI Foundry
        2. A ToolSet with all registered tool functions
        3. The agent with system prompt and model deployment
        """
        # Initialize the Agents SDK client
        # Docs: https://learn.microsoft.com/en-us/azure/ai-foundry/how-to/develop/sdk-overview
        self._client = AgentsClient(
            endpoint=self._config.agent_endpoint,
            credential=DefaultAzureCredential(),
        )

        # Build ToolSet from registered tool functions.
        # ToolSet enables auto function calling: the SDK automatically
        # executes tool functions when the agent requests them.
        # Docs: https://learn.microsoft.com/en-us/python/api/overview/azure/ai-agents-readme
        toolset = ToolSet()
        if self._tool_functions:
            functions = FunctionTool(self._tool_functions)
            toolset.add(functions)

        # Enable auto function calling so the SDK handles tool execution
        self._client.enable_auto_function_calls(toolset)

        # Load system prompt
        system_prompt = self._load_system_prompt()

        # Create the agent
        # Docs: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/quickstart
        self._agent = self._client.create_agent(
            model=self._config.model_deployment,
            name="customer-service-voice-agent",
            instructions=system_prompt,
            toolset=toolset,
        )
        logger.info(
            "Agent created: id=%s, model=%s",
            self._agent.id,
            self._config.model_deployment,
        )

    def create_thread(self) -> str:
        """Create a new conversation thread and return its ID.

        Each phone call / session should have its own thread to maintain
        conversation context throughout the interaction.
        """
        assert self._client is not None, "Client not initialized"
        thread = self._client.threads.create()
        logger.info("Thread created: %s", thread.id)
        return thread.id

    def process_message(self, thread_id: str, user_text: str) -> str:
        """Send a user message to the agent and get the response.

        This is the core conversation loop:
        1. Add the user's transcribed speech as a message
        2. Run the agent (which may call tools)
        3. Wait for completion
        4. Return the agent's text response (for TTS)

        Args:
            thread_id: The conversation thread ID.
            user_text: Transcribed speech text from Voice Live STT.

        Returns:
            The agent's text response to be sent to Voice Live TTS.
        """
        assert self._client is not None and self._agent is not None

        # Add the user message to the thread
        self._client.messages.create(
            thread_id=thread_id,
            role=MessageRole.USER,
            content=user_text,
        )

        # Run the agent - it will process the message, potentially call tools,
        # and generate a response
        run = self._client.runs.create_and_process(
            thread_id=thread_id,
            agent_id=self._agent.id,
        )
        logger.info("Agent run completed: status=%s", run.status)

        if run.status == "failed":
            logger.error("Agent run failed: %s", run.last_error)
            return "Entschuldigung, es ist ein Fehler aufgetreten. Bitte versuchen Sie es erneut."

        # Retrieve the latest agent message.
        # The SDK provides a text_messages helper on each message object.
        messages = self._client.messages.list(
            thread_id=thread_id,
            order=ListSortOrder.DESCENDING,
        )

        for msg in messages:
            if msg.role == MessageRole.AGENT and msg.text_messages:
                response_text = msg.text_messages[-1].text.value
                logger.info("Agent response: %s", response_text[:100])
                return response_text

        return "Entschuldigung, ich konnte Ihre Anfrage nicht verarbeiten."

    async def cleanup(self) -> None:
        """Delete the agent and free resources."""
        if self._client and self._agent:
            self._client.delete_agent(self._agent.id)
            logger.info("Agent deleted: %s", self._agent.id)

    def _load_system_prompt(self) -> str:
        """Load the system prompt from the prompts directory."""
        if _SYSTEM_PROMPT_PATH.exists():
            return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        # Fallback prompt
        return (
            "Du bist ein freundlicher Kundenservice-Agent. "
            "Beantworte Kundenanfragen höflich und effizient auf Deutsch. "
            "Nutze die verfügbaren Tools, um Kundendaten abzurufen, "
            "Termine zu buchen, Bestellungen zu prüfen und Tickets zu erstellen."
        )
