# -------------------------------------------------------------------------
# Voice Module - Real-time voice with non-blocking LangGraph integration
# -------------------------------------------------------------------------
"""
Voice module providing real-time voice conversation capabilities with
non-blocking LangGraph agent integration.

Architecture:
    VoiceService (Azure VoiceLive) ←→ VoiceAgentBridge ←→ LangGraph Agent
    
    - Voice conversations happen in real-time
    - Data queries spawn background agent tasks
    - Results are injected back when ready
    - Conversation continues without blocking

Components:
    VoiceService
        Core voice service using Azure AI VoiceLive for real-time
        speech-to-speech conversations.
        
    VoiceAgentBridge
        Non-blocking bridge between voice and LangGraph agent.
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
    # For Chainlit integration (see app/src/cl_voice_app.py)
    from voice.src.voice_service import VoiceService, VoiceServiceConfig
    from voice.src.voice_agent_bridge import VoiceAgentBridge, BridgeConfig
    
    # For CLI usage
    python voice/main_agent.py --endpoint <url> --use-token-credential
"""

# Core voice service
from voice.src.voice_service import (
    VoiceService,
    VoiceServiceConfig,
    VoiceEvent,
    VoiceEventType,
)

# Voice-Agent bridge
from voice.src.voice_agent_bridge import (
    VoiceAgentBridge,
    VoiceAgentBridgeBuilder,
    BridgeConfig,
)

# Query classification
from voice.src.query_classifier import (
    QueryClassifier,
    KeywordClassifier,
    ClassifierConfig,
    QueryType,
    ClassificationResult,
    create_classifier,
)

# Task management
from voice.src.pending_task_manager import (
    PendingTaskManager,
    TaskManagerConfig,
    PendingTask,
    TaskResult,
    TaskStatus,
)

# Basic assistant (CLI without agent)
from voice.src.basic_voice_assistant import BasicVoiceAssistant

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
]
