# Voice Live API

## Introduction

The Voice Live API is Azure's real-time speech processing service designed for interactive, low-latency voice applications. It combines speech-to-text, text-to-speech, voice activity detection, and noise suppression into a single WebSocket connection, making it the ideal transport layer for voice agents that need to hold natural, human-like conversations.

This document explains the core concepts of Voice Live, walks through the connection and session lifecycle, describes every important event, and provides annotated code examples so you can integrate it into your own applications.

> **Official documentation:** [Voice Live API Overview](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live)

---

## What is Voice Live API?

Traditional speech pipelines require you to stitch together separate STT, TTS, and VAD services, manage audio buffering, and handle turn-taking logic yourself. Voice Live removes that complexity by offering a single, bidirectional WebSocket endpoint that:

- **Streams audio in real time** -- send raw PCM frames and receive synthesised audio frames over the same connection.
- **Performs voice activity detection (VAD)** -- knows when the speaker starts and stops talking, including semantic-level understanding of pauses.
- **Suppresses background noise** -- cleans up audio before transcription, improving accuracy in noisy environments.
- **Synthesises speech** -- generates natural-sounding audio from the agent's text responses using Azure Neural Voices.

All of this happens with sub-second latency, making Voice Live suitable for telephony and WebRTC use cases.

> **Product page:** [Azure Speech in Foundry Tools](https://azure.microsoft.com/en-us/products/ai-foundry/tools/speech)

---

## Two Ways to Connect

There are two approaches to connect to Voice Live:

1. **Official SDK (recommended):** Use the `azure-ai-voicelive` package which handles connection, auth, and event typing automatically.
2. **Raw WebSocket:** Use the `websockets` library for full control over the connection.

This document covers both approaches. The SDK is simpler; the raw WebSocket gives you more control.

---

## Option 1: Official SDK (Recommended)

```python
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AzureStandardVoice, InputAudioFormat, Modality,
    OutputAudioFormat, RequestSession, ServerEventType, ServerVad,
    AudioNoiseReduction,
)
from azure.identity.aio import DefaultAzureCredential

credential = DefaultAzureCredential()

async with connect(endpoint=endpoint, credential=credential, model="gpt-4.1") as connection:
    # Configure the session
    await connection.session.update(session=RequestSession(
        modalities=[Modality.TEXT, Modality.AUDIO],
        voice=AzureStandardVoice(name="de-DE-ConradNeural"),
        input_audio_format=InputAudioFormat.PCM16,
        output_audio_format=OutputAudioFormat.PCM16,
        turn_detection=ServerVad(threshold=0.5, silence_duration_ms=500),
        input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
    ))

    # Send audio
    await connection.input_audio_buffer.append(audio=base64_audio)

    # Receive events (typed)
    async for event in connection:
        if event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            play_audio(event.delta)
```

> **Install:** `pip install azure-ai-voicelive[aiohttp] azure-identity`

---

## Option 2: Raw WebSocket

### Endpoint

The Voice Live endpoint follows this pattern:

```
wss://<resource>.services.ai.azure.com/voice-live/realtime?api-version=2025-10-01&model=<deployment>
```

Replace `<resource>` with the name of your Azure AI Foundry resource and `<deployment>` with your model deployment name (e.g., `gpt-4.1`).

### Authentication

You authenticate the WebSocket handshake using either:

1. **Bearer token** (recommended) -- obtained via `DefaultAzureCredential` or another `azure-identity` credential.
2. **API key** -- passed as a query parameter or header.

```python
import asyncio
import json
import websockets
from azure.identity import DefaultAzureCredential

async def connect_to_voice_live(resource_name: str) -> websockets.WebSocketClientProtocol:
    """
    Open an authenticated WebSocket connection to the Voice Live API.
    Uses DefaultAzureCredential, which picks up Azure CLI tokens locally
    and managed identity tokens in production.
    """
    # Obtain a bearer token scoped to Azure Cognitive Services
    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")

    # Build the WebSocket URL
    url = (
        f"wss://{resource_name}.services.ai.azure.com"
        f"/voice-live/realtime?api-version=2025-10-01"
    )

    # Connect with the Authorization header
    ws = await websockets.connect(
        url,
        additional_headers={
            "Authorization": f"Bearer {token.token}",
        },
    )
    return ws
```

> **Reference:** [Voice Live Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart)

---

## Session Configuration

After the WebSocket connection is established, the first message you send configures the session. This is where you specify the voice, audio format, VAD mode, and noise reduction.

```python
# Full session configuration with all features enabled
session_config = {
    "type": "session.update",
    "session": {
        # Enable both text and audio modalities so the API returns
        # transcriptions alongside synthesised audio
        "modalities": ["text", "audio"],

        # Voice settings for text-to-speech output
        "voice": {
            "type": "azure-standard",         # Use Azure Neural Voice
            "name": "de-DE-ConradNeural",     # German male voice
            "temperature": 0.8                # Controls expressiveness (0.6-1.2)
        },

        # Audio format for microphone input and speaker output
        "input_audio_format": "pcm16",        # 16-bit PCM, little-endian
        "output_audio_format": "pcm16",       # Same format for playback
        "input_audio_sampling_rate": 24000,   # 24 kHz sample rate

        # Voice Activity Detection -- determines when the speaker has
        # finished their turn. "azure_semantic_vad" is context-aware and
        # avoids cutting off mid-sentence pauses.
        "turn_detection": {
            "type": "azure_semantic_vad",
            "threshold": 0.5,                 # Sensitivity (0.0-1.0)
            "silence_duration_ms": 500         # Milliseconds of silence before end-of-turn
        },

        # Noise reduction -- removes background noise before STT processing.
        # "azure_deep_noise_suppression" uses a deep neural network model.
        "input_audio_noise_reduction": {
            "type": "azure_deep_noise_suppression"
        }
    }
}
```

### Configuration Parameters Explained

| Parameter | Value | Description |
|---|---|---|
| `modalities` | `["text", "audio"]` | Receive both transcribed text and synthesised audio. Use `["audio"]` if you only need audio output. |
| `voice.name` | `de-DE-ConradNeural` | Azure Neural Voice identifier. Use the [voice gallery](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts) to find other voices. |
| `voice.temperature` | `0.8` | Controls expressiveness (valid range: 0.6-1.2). Higher = more expressive, lower = more neutral. Default: 0.8. |
| `input_audio_format` | `pcm16` | Raw 16-bit signed integer PCM. No compression overhead, ideal for low-latency streaming. |
| `input_audio_sampling_rate` | `24000` | 24 kHz is the recommended rate for Voice Live. |
| `turn_detection.type` | `azure_semantic_vad` | Semantic VAD understands language structure and does not mistake brief pauses for turn endings. |
| `turn_detection.silence_duration_ms` | `500` | How long the speaker must be silent before the system considers the turn complete. |
| `input_audio_noise_reduction.type` | `azure_deep_noise_suppression` | Deep-learning-based noise cancellation. Especially useful in call-centre and mobile scenarios. |

---

## Events

Communication over the WebSocket is event-driven. Both the client and the server send JSON messages with a `type` field. Below are the most important events.

### Server-to-Client Events

| Event | When | Payload |
|---|---|---|
| `session.created` | Immediately after the WebSocket opens | Session ID, default configuration |
| `session.updated` | After processing a `session.update` | Confirmed configuration |
| `input_audio_buffer.speech_started` | VAD detects the speaker started talking | Timestamp |
| `input_audio_buffer.speech_stopped` | VAD detects the speaker stopped talking | Timestamp, duration |
| `conversation.item.created` | A new conversation item (user or assistant turn) is created | Item ID, role, content |
| `response.text.delta` | Incremental text from the agent's response | Text fragment |
| `response.audio.delta` | Incremental synthesised audio from TTS | Base64-encoded PCM16 chunk |
| `response.done` | The agent's response is complete | Full response metadata |
| `response.function_call` | The agent wants to call a tool | Function name, arguments (JSON) |

### Client-to-Server Events

| Event | When to Send | Payload |
|---|---|---|
| `session.update` | At the start to configure the session (or mid-session to change settings) | Session configuration object |
| `input_audio_buffer.append` | Continuously, as audio is captured from the microphone | Base64-encoded PCM16 chunk |
| `input_audio_buffer.commit` | When you want to force a turn end (optional -- VAD usually handles this) | -- |
| `conversation.item.create` | To inject a text message into the conversation programmatically | Role, content |
| `response.create` | To explicitly request a response from the agent | Optional configuration |
| `response.function_call_output` | After you have executed a tool locally and want to return the result | Call ID, output (JSON) |

---

## Code Examples

### Sending Audio

```python
import base64

async def stream_audio(ws: websockets.WebSocketClientProtocol, audio_chunk: bytes):
    """
    Send a chunk of raw PCM16 audio to the Voice Live API.

    The audio must be 16-bit signed integer PCM at the sample rate
    configured in the session (24 kHz by default).
    """
    message = {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(audio_chunk).decode("ascii"),
    }
    await ws.send(json.dumps(message))
```

### Receiving Events

```python
async def receive_events(ws: websockets.WebSocketClientProtocol):
    """
    Listen for events from the Voice Live API and handle them.
    This is the main event loop that runs for the lifetime of the session.
    """
    async for raw_message in ws:
        event = json.loads(raw_message)
        event_type = event.get("type", "")

        if event_type == "session.created":
            # The session is ready -- we can start sending audio
            print(f"Session created: {event['session']['id']}")

        elif event_type == "response.text.delta":
            # The agent is generating text incrementally
            print(event["delta"], end="", flush=True)

        elif event_type == "response.audio.delta":
            # Decode and play the synthesised audio chunk
            audio_bytes = base64.b64decode(event["delta"])
            play_audio(audio_bytes)  # your audio playback function

        elif event_type == "response.function_call":
            # The agent wants to call a tool -- execute it and return the result
            result = await execute_tool(
                event["name"],
                json.loads(event["arguments"]),
            )
            await ws.send(json.dumps({
                "type": "response.function_call_output",
                "call_id": event["call_id"],
                "output": json.dumps(result),
            }))

        elif event_type == "response.done":
            print("\n--- Agent response complete ---")
```

### Full Session Lifecycle

```python
async def run_voice_session(resource_name: str):
    """
    Complete Voice Live session: connect, configure, stream audio,
    and handle responses.
    """
    # 1. Connect to the Voice Live WebSocket
    ws = await connect_to_voice_live(resource_name)

    # 2. Configure the session (voice, VAD, noise suppression)
    await ws.send(json.dumps(session_config))

    # 3. Wait for session.created confirmation
    created_event = json.loads(await ws.recv())
    assert created_event["type"] == "session.created"
    print(f"Connected to Voice Live session: {created_event['session']['id']}")

    # 4. Run audio input and event handling concurrently
    await asyncio.gather(
        capture_and_stream_audio(ws),   # reads from microphone, sends audio
        receive_events(ws),             # handles all server events
    )
```

### Example Dialogue (German)

A typical interaction through the system:

```
Kunde:  "Guten Tag, ich habe ein Problem mit meiner letzten Lieferung."
         → input_audio_buffer.append (streamed PCM16 frames)
         → input_audio_buffer.speech_stopped (VAD detects end of turn)
         → conversation.item.created (transcribed user message)

Agent:  → response.function_call: ticket_tool.create_ticket(
              description="Problem mit letzter Lieferung",
              priority="high"
          )
        ← response.function_call_output: {"ticket_id": "T-2025-4711", "status": "created"}
        → response.text.delta: "Ich habe ein Ticket mit der Nummer T-2025-4711 erstellt..."
        → response.audio.delta: <synthesised audio chunks>
        → response.done

Kunde hört: "Ich habe ein Ticket mit der Nummer T-2025-4711 fuer Sie erstellt.
              Ein Mitarbeiter wird sich innerhalb von 24 Stunden bei Ihnen melden."
```

---

## Troubleshooting Hints

- **WebSocket closes immediately (HTTP 401):** Your token is expired or missing the required scope. Make sure you request a token for `https://cognitiveservices.azure.com/.default`.
- **No `session.created` event:** The API version in the URL may be wrong. Use `2025-10-01`.
- **Audio sounds garbled:** Confirm both sides use the same format (`pcm16`) and sample rate (`24000`). Mismatched rates cause pitch and speed distortion.
- **VAD triggers too early / too late:** Adjust `threshold` and `silence_duration_ms` in the session configuration. Lower threshold = more sensitive; higher silence duration = longer wait before end-of-turn.
- **Noise suppression not working:** Ensure `input_audio_noise_reduction.type` is set to `azure_deep_noise_suppression` in the session config. It is not enabled by default.

---

## Further Resources

- [Voice Live API Overview](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live)
- [Voice Live Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart)
- [Voice Live API Reference](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-api-reference)
- [Voice Live How-To Guide](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to)
- [Voice Live Agent Integration Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart)
- [Voice Live FAQ](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-faq)
- [Azure Speech in Foundry Tools](https://azure.microsoft.com/en-us/products/ai-foundry/tools/speech)
- [Azure Speech Service Documentation](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/)
