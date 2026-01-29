# Example 01: Basic Voice Live API Connection

This example demonstrates a minimal connection to the Azure Voice Live API.

## What it does

1. Connects to the Voice Live WebSocket endpoint
2. Configures a session with German voice (`de-DE-ConradNeural`)
3. Sends a text message (simulating speech input)
4. Logs all events received from Voice Live

## Run

```bash
# From project root
cp .env.example .env
# Edit .env with your Azure credentials

python examples/01_voice_live_basic/main.py
```

## Learn more

- [Voice Live Overview](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live)
- [Voice Live Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart)
