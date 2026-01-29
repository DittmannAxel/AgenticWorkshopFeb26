# Example 03: Voice Agent Integration

This example demonstrates the **full integration** of Voice Live API with the Foundry Agent Service.

## Architecture

```
Customer Audio → Voice Live (STT) → SessionManager → Agent (LLM + Tools) → SessionManager → Voice Live (TTS) → Customer Audio
```

## What it does

1. Creates a `SessionManager` that bridges Voice Live and the Agent
2. Connects to Voice Live for audio processing
3. Initializes the Foundry Agent with all customer service tools
4. Demonstrates the end-to-end flow with a simulated input

## Run

```bash
python examples/03_voice_agent_integration/main.py
```

## Learn more

- [Voice Live + Agents](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart)
