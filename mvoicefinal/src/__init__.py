# -------------------------------------------------------------------------
# Voice Module - Real-time voice with a non-blocking bridge
# -------------------------------------------------------------------------
"""
Voice module providing real-time voice conversation capabilities with a
non-blocking bridge for background lookups.

Architecture:
    VoiceService (Azure VoiceLive) ←→ VoiceAgentBridge ←→ Backend Agent
    
    - Voice conversations happen in real-time
    - Data queries spawn background agent tasks
    - Results are injected back when ready
    - Conversation continues without blocking

Components:
    VoiceService
        Core voice service using Azure AI VoiceLive for real-time
        speech-to-speech conversations.
        
    VoiceAgentBridge
        Non-blocking bridge between voice and a backend agent.
        Routes queries, manages background tasks, injects results.
        
    QueryClassifier
        Classifies queries to determine routing (simple vs data lookup).
        
    PendingTaskManager
        Manages background async tasks with lifecycle tracking.
        
    AudioProcessor
        PyAudio-based capture/playback for CLI usage.
        
    BasicVoiceAssistant
        Simple CLI voice assistant without agent integration.

Usage:
    python main_agent.py --endpoint <url> --use-token-credential
"""

from .voice_service import (
    VoiceService,
    VoiceServiceConfig,
    VoiceEvent,
    VoiceEventType,
)

from .voice_agent_bridge import (
    VoiceAgentBridge,
    VoiceAgentBridgeBuilder,
    BridgeConfig,
)

from .query_classifier import (
    QueryClassifier,
    KeywordClassifier,
    ClassifierConfig,
    QueryType,
    ClassificationResult,
    create_classifier,
)

from .pending_task_manager import (
    PendingTaskManager,
    TaskManagerConfig,
    PendingTask,
    TaskResult,
    TaskStatus,
)

from .basic_voice_assistant import BasicVoiceAssistant

from .order_agent import OrderAgent
from .order_backend import JsonFileOrderBackend, HttpOrderBackend

__all__ = [
    # Voice service
    "VoiceService",
    "VoiceServiceConfig",
    "VoiceEvent",
    "VoiceEventType",
    # Bridge
    "VoiceAgentBridge",
    "VoiceAgentBridgeBuilder", 
    "BridgeConfig",
    # Classifier
    "QueryClassifier",
    "KeywordClassifier",
    "ClassifierConfig",
    "QueryType",
    "ClassificationResult",
    "create_classifier",
    # Task manager
    "PendingTaskManager",
    "TaskManagerConfig",
    "PendingTask",
    "TaskResult",
    "TaskStatus",
    # Assistants
    "BasicVoiceAssistant",
    "OrderAgent",
    "JsonFileOrderBackend",
    "HttpOrderBackend",
]
