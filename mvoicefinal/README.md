# Voice Assistant

A voice assistant powered by Azure AI VoiceLive SDK with **non-blocking LangGraph agent integration** for real-time voice conversations.

## Features

- 🎤 Real-time voice input/output using PyAudio (CLI) or WebRTC (Chainlit)
- 🤖 Powered by Azure AI VoiceLive with GPT Realtime model
- 🔄 **Non-blocking agent queries** - conversation continues while data is fetched
- 🔐 Supports both API key and Azure token credential authentication
- 🎯 Configurable voice and system instructions
- 🔌 Modular architecture for easy integration

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  User Interface (Chainlit Web / CLI with PyAudio)                   │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────┐
│  VoiceService (Azure AI VoiceLive)                                  │
│  • Real-time speech-to-speech                                       │
│  • VAD, echo cancellation, noise reduction                          │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────┐
│  VoiceAgentBridge                                                   │
│  • Query classification (simple vs data lookup)                     │
│  • Spawns background agent tasks                                    │
│  • Injects results back into voice session                          │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────┐
│  LangGraph Agent (Background)                                       │
│  • Tool calls (Databricks, MCP)                                     │
│  • Runs async without blocking voice                                │
└─────────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Python 3.12+
- Azure AI VoiceLive resource (gpt-realtime model)
- For CLI: Microphone and speakers (PyAudio)
- For Chainlit: Modern browser with WebRTC support

## Installation

```bash
# Install dependencies using uv
uv sync
```

## Quick Start

### Option 1: CLI with Order Agent (Recommended)

```bash
# Using Azure CLI credential
python main_agent.py --endpoint <url> --use-token-credential

# Using API key
python main_agent.py --api-key <key> --endpoint <url>
```

### Option 2: Basic CLI (No Agent)

```bash
python main.py --api-key YOUR_API_KEY
```

### Optional: Run a Local Mock Orders API

```bash
python -m src.mock_orders_api --port 8083 --data kundendaten.json
python main_agent.py --endpoint <url> --use-token-credential --orders-service-url http://localhost:8083
```
	
## Environment Variables

```env
# Create a local env file:
#   cp .env.example .env
#
# Required: Azure VoiceLive
AZURE_VOICELIVE_ENDPOINT=https://your-endpoint.services.ai.azure.com/
AZURE_VOICELIVE_API_KEY=your-api-key  # Or use managed identity

# Optional: Voice configuration
AZURE_VOICELIVE_MODEL=gpt-realtime
AZURE_VOICELIVE_VOICE=en-US-Ava:DragonHDLatestNeural

# Optional: Agent bridge settings
VOICE_AGENT_TIMEOUT=30
VOICE_MAX_CONCURRENT_QUERIES=3
VOICE_ENABLED=true

# Optional: Local customer/order data (if ORDERS_SERVICE_URL is unset)
KUNDENDATEN_PATH=kundendaten.json

# Optional: Order lookup backend (if set, uses HTTP service instead of the JSON file)
ORDERS_SERVICE_URL=http://localhost:8083
```

## Configuration Options

| Argument | Description | Default |
|----------|-------------|---------|
| `--api-key` | Azure VoiceLive API key | `AZURE_VOICELIVE_API_KEY` env var |
| `--endpoint` | Azure VoiceLive endpoint | `AZURE_VOICELIVE_ENDPOINT` env var |
| `--model` | VoiceLive model | `gpt-realtime` |
| `--voice` | Voice for the assistant | `en-US-Ava:DragonHDLatestNeural` |
| `--instructions` | System instructions | Default helpful assistant prompt |
| `--orders-service-url` | HTTP base URL for order lookup | `ORDERS_SERVICE_URL` env var |
| `--use-token-credential` | Use Azure CLI credential | `false` |
| `--verbose` | Enable verbose logging | `false` |

## Programmatic Usage

The `VoiceService` class provides a modular interface for integrating voice capabilities into your applications.

### Basic Usage

```python
import asyncio
from azure.identity.aio import AzureCliCredential
from src.voice_service import VoiceService, VoiceServiceConfig, VoiceEvent, VoiceEventType

async def main():
    # Configure the voice service
    config = VoiceServiceConfig(
        endpoint="https://your-endpoint.services.ai.azure.com/",
        model="gpt-realtime",
        voice="en-US-Ava:DragonHDLatestNeural",
        instructions="You are a helpful AI assistant.",
    )

    # Create service with Azure credential
    credential = AzureCliCredential()
    service = VoiceService(credential, config)

    # Register event handler
    async def handle_event(event: VoiceEvent):
        if event.type == VoiceEventType.TRANSCRIPT:
            role = event.data.get("role", "unknown")
            text = event.data.get("transcript", "")
            print(f"[{role}]: {text}")
        elif event.type == VoiceEventType.RESPONSE_AUDIO:
            audio_bytes = event.data.get("audio")
            # Handle audio playback...

    service.on_event(handle_event)

    # Start the session
    await service.start()

    # Send audio data (PCM16, 24kHz, mono)
    await service.send_audio(audio_bytes)

    # Inject context (e.g., from an agent)
    await service.inject_context("User is asking about weather in Seattle")

    # Stop when done
    await service.stop()

asyncio.run(main())
```

### Event Types

| Event Type | Description | Data Fields |
|------------|-------------|-------------|
| `SESSION_STARTED` | Voice session connected | `model` |
| `SESSION_ENDED` | Voice session closed | - |
| `SPEECH_STARTED` | User started speaking | - |
| `SPEECH_ENDED` | User stopped speaking | - |
| `RESPONSE_STARTED` | Assistant response began | - |
| `RESPONSE_AUDIO` | Audio chunk from assistant | `audio` (bytes) |
| `RESPONSE_ENDED` | Assistant response complete | - |
| `TRANSCRIPT` | Transcript available | `role`, `transcript` |
| `ERROR` | Error occurred | `error` |

### Configuration Options

```python
from azure.ai.voicelive.models import InputAudioFormat, OutputAudioFormat

config = VoiceServiceConfig(
    endpoint="https://...",
    model="gpt-realtime",
    voice="en-US-Ava:DragonHDLatestNeural",
    instructions="You are a helpful assistant.",
    
    # VAD (Voice Activity Detection) settings
    vad_threshold=0.5,
    vad_prefix_padding_ms=300,
    vad_silence_duration_ms=500,
    
    # Audio settings
    input_format=InputAudioFormat.PCM16,
    output_format=OutputAudioFormat.PCM16,
    enable_echo_cancellation=True,
    enable_noise_reduction=True,
)
```

### Integration with Chainlit

```python
import chainlit as cl
from src.voice_service import VoiceService, VoiceServiceConfig, VoiceEvent, VoiceEventType

@cl.on_chat_start
async def on_chat_start():
    config = VoiceServiceConfig(endpoint="...", model="gpt-realtime")
    service = VoiceService(credential, config)
    
    async def handle_event(event: VoiceEvent):
        if event.type == VoiceEventType.RESPONSE_AUDIO:
            # Stream audio back to browser
            await cl.Audio(content=event.data["audio"]).send()
        elif event.type == VoiceEventType.TRANSCRIPT:
            await cl.Message(content=event.data["transcript"]).send()
    
    service.on_event(handle_event)
    await service.start()
    cl.user_session.set("voice_service", service)

@cl.on_audio_chunk
async def on_audio_chunk(chunk: cl.AudioChunk):
    service = cl.user_session.get("voice_service")
    await service.send_audio(chunk.data)
```

### Integration with LangGraph Agent

```python
from src.voice_service import VoiceService, VoiceEvent, VoiceEventType

async def voice_with_agent(service: VoiceService, agent):
    """Bridge voice service with LangGraph agent."""
    
    async def handle_event(event: VoiceEvent):
        if event.type == VoiceEventType.TRANSCRIPT:
            if event.data.get("role") == "user":
                # Route user speech to agent for processing
                user_text = event.data["transcript"]
                result = await agent.ainvoke({"messages": [user_text]})
                
                # Inject agent's context back into voice conversation
                agent_context = result.get("context", "")
                if agent_context:
                    await service.inject_context(agent_context)
    
    service.on_event(handle_event)
```

## Project Structure

```
./
├── main.py                     # CLI entry point
├── main_agent.py               # CLI entry point (order agent)
├── pyproject.toml
├── README.md
└── src/
    ├── voice_service.py        # Core VoiceService (integration-ready)
    ├── voice_agent_bridge.py   # Transcript -> agent -> speak result
    ├── basic_voice_assistant.py # CLI wrapper with PyAudio
    ├── audio_processor.py      # PyAudio capture/playback
    ├── order_agent.py          # Multi-turn order-status logic
    ├── order_backend.py        # HTTP + in-memory mock backend
    ├── mock_orders_api.py      # Optional local mock HTTP server
    └── set_logging.py          # Logging configuration
```

## License

MIT
