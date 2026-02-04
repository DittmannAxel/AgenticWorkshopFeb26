# -------------------------------------------------------------------------
# Agent Voice Assistant - CLI Entry Point
# -------------------------------------------------------------------------
"""
Command-line voice assistant with non-blocking order lookup integration.

This provides a standalone CLI for voice conversations that:
- Uses PyAudio for local microphone/speaker I/O
- Processes speech through Azure VoiceLive
- Routes order-status queries to a background lookup
- Continues conversation while the lookup runs

Usage:
    python main_agent.py --endpoint <url> --use-token-credential
    python main_agent.py --api-key <key> --endpoint <url>

Environment Variables:
    AZURE_VOICELIVE_ENDPOINT - VoiceLive endpoint URL
    AZURE_VOICELIVE_API_KEY - API key (or use --use-token-credential)
    AZURE_VOICELIVE_MODEL - Model name (default: gpt-realtime)
    AZURE_VOICELIVE_VOICE - Voice name (default: en-US-Ava:DragonHDLatestNeural)
"""
from __future__ import annotations

import os
import sys
import argparse
import asyncio
import signal
from pathlib import Path
from typing import Union, Optional, cast

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent / ".env")

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential
import pyaudio

from src.voice_service import VoiceService, VoiceServiceConfig, VoiceEvent, VoiceEventType
from src.voice_agent_bridge import VoiceAgentBridge, BridgeConfig
from src.audio_processor import AudioProcessor
from src.set_logging import logger
from src.order_agent import OrderAgent
from src.order_backend import MockOrderBackend, HttpOrderBackend


# -------------------------------------------------------------------------
# Voice Instructions for Agent Integration
# -------------------------------------------------------------------------

AGENT_VOICE_INSTRUCTIONS = """Sie sind ein professioneller Kundenservice-Assistent.

WICHTIGE REGELN:
1. Wenn der Kunde nach einer Bestellung fragt und keine Bestellnummer (z.B. ORD-5001) oder kein Name vorliegt, fragen Sie danach.
2. Sobald Bestellnummer oder Name vorliegt, beantworten Sie die Frage anhand des Zusatzkontexts.
3. Antworten Sie kurz und gut verständlich (Voice).
4. Verwenden Sie die Sie-Form.

STIL:
- Freundlich, klar, keine langen Listen.
- Wenn Sie etwas nicht finden, fragen Sie nach der Bestellnummer oder dem Namen.
"""


# -------------------------------------------------------------------------
# Agent Voice Assistant
# -------------------------------------------------------------------------

class AgentVoiceAssistant:
    """
    CLI voice assistant with a non-blocking backend lookup bridge.
    
    This assistant combines:
    - VoiceService for real-time speech (Azure VoiceLive)
    - AudioProcessor for local microphone/speaker (PyAudio)
    - VoiceAgentBridge for background agent queries
    """
    
    def __init__(
        self,
        endpoint: str,
        credential: Union[AzureKeyCredential, AsyncTokenCredential],
        model: str = "gpt-realtime",
        voice: str = "en-US-Ava:DragonHDLatestNeural",
        instructions: Optional[str] = None,
        agent_timeout: float = 30.0,
        orders_service_url: Optional[str] = None,
    ):
        # Voice service config
        config = VoiceServiceConfig(
            endpoint=endpoint,
            model=model,
            voice=voice,
            instructions=instructions or AGENT_VOICE_INSTRUCTIONS,
        )
        
        self.voice_service = VoiceService(credential, config)
        self.agent_timeout = agent_timeout
        self.orders_service_url = orders_service_url
        
        # Components initialized in start()
        self.audio_processor: Optional[AudioProcessor] = None
        self.bridge: Optional[VoiceAgentBridge] = None
        self.agent = None
        
        # State tracking
        self._agent_working = False
        
        # Register voice event handlers for CLI output
        self.voice_service.on_event(self._handle_voice_event)
    
    async def start(self) -> None:
        """Start the voice assistant with agent integration."""
        try:
            logger.info("Starting AgentVoiceAssistant")
            
            # Create deterministic order agent (no external LangGraph dependency)
            orders_url = (self.orders_service_url or os.environ.get("ORDERS_SERVICE_URL") or "").strip()
            backend = HttpOrderBackend(orders_url) if orders_url else MockOrderBackend()
            self.agent = OrderAgent(backend)
            print("✅ Order agent ready")
            
            # Start voice service
            print("🎤 Connecting to Azure VoiceLive...")
            await self.voice_service.start()
            
            # Initialize audio processor
            self.audio_processor = AudioProcessor(self.voice_service.connection)
            
            # Create and start bridge
            bridge_config = BridgeConfig(
                max_concurrent_queries=3,
                agent_timeout=self.agent_timeout,
            )
            bridge_config.interrupt_playback = self._interrupt_playback
            
            self.bridge = VoiceAgentBridge(
                voice_service=self.voice_service,
                agent=self.agent,
                config=bridge_config,
            )
            
            # Register bridge callbacks for CLI feedback
            self.bridge.on_agent_start(self._on_agent_started)
            self.bridge.on_agent_complete(self._on_agent_completed)
            self.bridge.on_agent_error(self._on_agent_error)
            
            await self.bridge.start()
            
            # Start audio
            self.audio_processor.start_playback()
            self.audio_processor.start_capture()
            
            # Ready!
            logger.info("AgentVoiceAssistant ready")
            print("\n" + "=" * 60)
            print("🎤 AGENT VOICE ASSISTANT READY")
            print("=" * 60)
            print("Features:")
            print("  • Real-time voice (Azure VoiceLive)")
            print("  • Non-blocking order lookups (order id or name)")
            print("-" * 60)
            print("Start speaking to begin conversation")
            print("Press Ctrl+C to exit")
            print("=" * 60 + "\n")
            
            # Wait for voice service to complete
            event_task = self.voice_service.event_task
            if event_task:
                await event_task
                
        finally:
            await self.shutdown()
    
    async def shutdown(self) -> None:
        """Clean up all resources."""
        logger.info("Shutting down AgentVoiceAssistant")
        
        # Stop bridge first (cancels pending tasks)
        if self.bridge:
            await self.bridge.stop()
            self.bridge = None
        
        # Stop audio
        if self.audio_processor:
            self.audio_processor.shutdown()
            self.audio_processor = None
        
        # Stop voice service
        await self.voice_service.stop()
    
    async def _handle_voice_event(self, event: VoiceEvent) -> None:
        """Handle voice events for CLI output."""
        if event.type == VoiceEventType.SPEECH_STARTED:
            status = "🎤 Listening..."
            if self._agent_working:
                status += " (🔍 Agent working in background)"
            print(status)
            
            # Handle barge-in - clear audio
            if self.audio_processor:
                self.audio_processor.skip_pending_audio()
        
        elif event.type == VoiceEventType.SPEECH_ENDED:
            print("⏳ Processing...")
        
        elif event.type == VoiceEventType.RESPONSE_STARTED:
            status = "🤖 Assistant speaking"
            if self._agent_working:
                status += " (🔍 Agent still working)"
            print(status)
        
        elif event.type == VoiceEventType.RESPONSE_AUDIO:
            # Route audio to playback
            if self.audio_processor:
                audio_bytes = event.data.get("audio")
                if audio_bytes:
                    self.audio_processor.queue_audio(audio_bytes)
        
        elif event.type == VoiceEventType.RESPONSE_ENDED:
            pending = self.bridge.pending_query_count if self.bridge else 0
            if pending > 0:
                print(f"💬 Ready ({pending} lookup(s) in progress)")
            else:
                print("💬 Ready")
        
        elif event.type == VoiceEventType.TRANSCRIPT:
            role = event.data.get("role", "unknown")
            text = event.data.get("transcript", "")
            if text:
                print(f"[{role}]: {text}")
        
        elif event.type == VoiceEventType.ERROR:
            error = event.data.get("error", "Unknown error")
            print(f"❌ Error: {error}")
    
    async def _on_agent_started(self, query: str) -> None:
        """Called when agent starts processing."""
        self._agent_working = True
        print(f"🔍 Agent looking up: {query[:50]}{'...' if len(query) > 50 else ''}")
    
    async def _on_agent_completed(self, query: str, response: str) -> None:
        """Called when agent completes."""
        self._agent_working = (
            self.bridge.pending_query_count > 0 if self.bridge else False
        )
        print(f"✅ Agent found data for: {query[:50]}{'...' if len(query) > 50 else ''}")
    
    async def _on_agent_error(self, query: str, error: Exception) -> None:
        """Called when agent encounters an error."""
        self._agent_working = (
            self.bridge.pending_query_count > 0 if self.bridge else False
        )
        print(f"⚠️ Agent error for '{query[:30]}...': {error}")

    async def _interrupt_playback(self) -> None:
        """Stop queued audio immediately (so injected results are heard right away)."""
        if self.audio_processor:
            self.audio_processor.skip_pending_audio()


# -------------------------------------------------------------------------
# CLI Entry Point
# -------------------------------------------------------------------------

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Agent Voice Assistant - Voice with order lookup agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    parser.add_argument(
        "--api-key",
        help="Azure VoiceLive API key",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_API_KEY"),
    )
    
    parser.add_argument(
        "--endpoint",
        help="Azure VoiceLive endpoint",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_ENDPOINT"),
    )
    
    parser.add_argument(
        "--model",
        help="VoiceLive model",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_MODEL", "gpt-realtime"),
    )
    
    parser.add_argument(
        "--voice",
        help="Voice to use",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_VOICE", "en-US-Ava:DragonHDLatestNeural"),
    )
    
    parser.add_argument(
        "--timeout",
        help="Agent query timeout in seconds",
        type=float,
        default=float(os.environ.get("VOICE_AGENT_TIMEOUT", "30")),
    )

    parser.add_argument(
        "--orders-service-url",
        help="Optional HTTP base URL for order lookup (else uses in-memory mock).",
        type=str,
        default=os.environ.get("ORDERS_SERVICE_URL"),
    )
    
    parser.add_argument(
        "--use-token-credential",
        help="Use Azure CLI credential instead of API key",
        action="store_true",
        default=False,
    )
    
    parser.add_argument(
        "--verbose",
        help="Enable verbose logging",
        action="store_true",
    )
    
    return parser.parse_args()


def check_audio_devices() -> bool:
    """Verify audio devices are available."""
    try:
        p = pyaudio.PyAudio()
        
        input_devices = [
            i for i in range(p.get_device_count())
            if cast(Union[int, float], p.get_device_info_by_index(i).get("maxInputChannels", 0) or 0) > 0
        ]
        output_devices = [
            i for i in range(p.get_device_count())
            if cast(Union[int, float], p.get_device_info_by_index(i).get("maxOutputChannels", 0) or 0) > 0
        ]
        p.terminate()
        
        if not input_devices:
            print("❌ No audio input devices found. Please check your microphone.")
            return False
        if not output_devices:
            print("❌ No audio output devices found. Please check your speakers.")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Audio system check failed: {e}")
        return False


async def run_assistant(args) -> None:
    """Run the voice assistant."""
    # Validate credentials
    if not args.api_key and not args.use_token_credential:
        print("❌ Error: No authentication provided")
        print("Use --api-key or set AZURE_VOICELIVE_API_KEY")
        print("Or use --use-token-credential for Azure CLI authentication")
        return
    
    if not args.endpoint:
        print("❌ Error: No endpoint provided")
        print("Use --endpoint or set AZURE_VOICELIVE_ENDPOINT")
        return
    
    # Create credential
    credential: Union[AzureKeyCredential, AsyncTokenCredential]
    if args.use_token_credential:
        credential = AzureCliCredential()
        logger.info("Using Azure CLI credential")
    else:
        credential = AzureKeyCredential(args.api_key)
        logger.info("Using API key credential")
    
    # Create assistant
    assistant = AgentVoiceAssistant(
        endpoint=args.endpoint,
        credential=credential,
        model=args.model,
        voice=args.voice,
        agent_timeout=args.timeout,
        orders_service_url=args.orders_service_url,
    )
    
    # Setup signal handlers
    def signal_handler(_sig, _frame):
        logger.info("Received shutdown signal")
        raise KeyboardInterrupt()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run
    try:
        await assistant.start()
    except KeyboardInterrupt:
        print("\n👋 Voice assistant shut down. Goodbye!")


def main():
    """Main entry point."""
    args = parse_arguments()
    
    if args.verbose:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Check audio
    if not check_audio_devices():
        sys.exit(1)
    
    print("🎙️  Agent Voice Assistant")
    print("=" * 50)
    
    # Run
    asyncio.run(run_assistant(args))


if __name__ == "__main__":
    main()
