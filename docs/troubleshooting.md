# Troubleshooting Guide

## Introduction

This guide covers the most common problems you may encounter when running the Azure AI Foundry Voice Agent Demo, along with their causes and solutions. It is organised by problem category so you can jump directly to the section relevant to your issue.

Before diving into specific issues, make sure you have completed all steps in the [Setup Guide](setup-guide.md) and that your environment variables in `.env` are correctly configured.

---

## Authentication Issues

Authentication is the most frequent source of errors. The demo uses `DefaultAzureCredential` from the `azure-identity` SDK, which tries multiple credential sources in order.

### Problem: `DefaultAzureCredential` fails with "No credential matched"

**Symptoms:**
```
azure.identity.CredentialUnavailableError: DefaultAzureCredential failed to retrieve a token from the included credentials.
```

**Causes and Solutions:**

1. **Azure CLI not logged in** -- Run `az login` and ensure the correct subscription is selected:
   ```bash
   az login
   az account set --subscription "<your-subscription-id>"
   ```

2. **Token expired** -- Azure CLI tokens expire after a few hours. Run `az login` again.

3. **Wrong subscription** -- Your AI Foundry resource may be in a different subscription than the one currently selected:
   ```bash
   # List all subscriptions
   az account list --output table

   # Switch to the correct one
   az account set --subscription "<subscription-id>"
   ```

### Problem: API key authentication returns 401

**Symptoms:**
```
HTTP 401 Unauthorized
```

**Solutions:**
- Verify the API key in your `.env` file is correct and has not been regenerated in the Azure portal.
- Ensure you are using the correct key (Key 1 or Key 2 from the resource's "Keys and Endpoint" page).
- Check that the key matches the resource in your `AZURE_FOUNDRY_ENDPOINT`.

### Problem: Managed Identity not working in production

**Symptoms:** The application works locally but fails with authentication errors when deployed to Azure.

**Solutions:**
- Ensure the managed identity (system-assigned or user-assigned) is enabled on your hosting resource (App Service, Container Instance, AKS).
- Assign the required RBAC role to the managed identity:
  ```bash
  # Grant "Cognitive Services User" role on the AI Foundry resource
  az role assignment create \
    --assignee "<managed-identity-object-id>" \
    --role "Cognitive Services User" \
    --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<resource>"
  ```
- The role assignment can take up to 10 minutes to propagate.

> **Reference:** [DefaultAzureCredential documentation](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential)

---

## WebSocket Connection Failures

### Problem: WebSocket connection immediately closes

**Symptoms:**
```
websockets.exceptions.InvalidStatusCode: server rejected WebSocket connection: HTTP 401
```

**Causes:**
- The bearer token is missing, expired, or scoped incorrectly.
- The API key is wrong.

**Solutions:**
```python
# Make sure you request a token with the correct scope
credential = DefaultAzureCredential()
token = credential.get_token("https://cognitiveservices.azure.com/.default")

# Verify the token is not None and print its expiry for debugging
print(f"Token expires at: {token.expires_on}")
```

### Problem: WebSocket connection times out

**Symptoms:** The connection hangs and eventually times out without receiving `session.created`.

**Causes:**
- Network firewall or proxy blocking WebSocket connections (`wss://`).
- Incorrect resource name in the endpoint URL.

**Solutions:**
- Verify the endpoint URL format: `wss://<resource>.services.ai.azure.com/voice-live/realtime?api-version=2025-10-01&model=<AZURE_VOICELIVE_MODEL>`
- Test basic connectivity:
  ```bash
  # Check DNS resolution
  nslookup <resource>.services.ai.azure.com

  # Check if the HTTPS endpoint is reachable
  curl -I "https://<resource>.services.ai.azure.com"
  ```
- If behind a corporate proxy, ensure WebSocket upgrades are allowed.

### Problem: WebSocket closes mid-session with error 1008

**Symptoms:** The connection drops during a conversation with a policy violation error.

**Causes:**
- Sending malformed JSON.
- Sending audio in the wrong format.
- Session idle timeout exceeded.

**Solutions:**
- Validate your JSON before sending: `json.loads(json.dumps(message))`.
- Ensure audio chunks are Base64-encoded PCM16.
- Send periodic audio frames (even silence) to keep the session alive.

---

## Audio Format Mismatches

### Problem: Audio output sounds garbled, too fast, or too slow

**Symptoms:** The synthesised speech is unintelligible or plays at the wrong speed.

**Causes:** The sample rate or encoding of the audio being played does not match the format configured in the session.

**Solutions:**
- Ensure your session configuration matches your playback setup:
  ```python
  # Both input and output must use the same format
  "input_audio_format": "pcm16",        # 16-bit signed integer, little-endian
  "output_audio_format": "pcm16",
  "input_audio_sampling_rate": 24000,    # 24 kHz
  ```
- When playing audio, configure your audio library to use 24000 Hz, 16-bit, mono:
  ```python
  import pyaudio

  # PyAudio playback configuration matching Voice Live output
  stream = pyaudio.PyAudio().open(
      format=pyaudio.paInt16,     # 16-bit PCM
      channels=1,                  # Mono
      rate=24000,                  # Must match session config
      output=True,
  )
  ```

### Problem: STT produces no transcriptions or wrong language

**Symptoms:** The `conversation.item.created` events contain empty or incorrect text.

**Solutions:**
- Verify the input audio is actually PCM16 at 24 kHz (not MP3, WAV headers, or a different sample rate).
- Check that the voice/language configured matches the speaker's language. For German input, use a German voice like `de-DE-ConradNeural`.
- Increase the microphone volume or check that noise suppression is not overly aggressive.

---

## Voice Live Session Errors

### Problem: `session.update` rejected

**Symptoms:** After sending the session configuration, you receive an error event instead of `session.updated`.

**Solutions:**
- Check for typos in the configuration keys. Common mistakes:
  ```python
  # Wrong                              # Correct
  "input_audio_noise_supression"       "input_audio_noise_reduction"
  "turn_detect"                        "turn_detection"
  "voice_name"                         "voice": {"name": "..."}
  ```
- Ensure all required fields are present in the session object.
- Use the exact values from the [API reference](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-api-reference).

### Problem: VAD does not detect end of turn

**Symptoms:** The agent never responds because the system does not recognise that the speaker has stopped talking.

**Solutions:**
- Lower the VAD threshold to make it more sensitive:
  ```python
  "turn_detection": {
      "type": "azure_semantic_vad",
      "threshold": 0.3,              # Lower = more sensitive (default 0.5)
      "silence_duration_ms": 300     # Shorter silence triggers end-of-turn
  }
  ```
- As a workaround, manually commit the audio buffer after a timeout:
  ```python
  await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
  ```

---

## Agent Tool Execution Failures

### Problem: Agent never calls tools

**Symptoms:** The agent responds with generic text instead of calling the expected tool.

**Causes:**
- Tool descriptions are too vague for the LLM to match the user's intent.
- Tools are not registered when creating the agent.

**Solutions:**
- Improve tool docstrings and parameter descriptions:
  ```python
  # Vague -- the LLM may not know when to use this
  def get_status(id: str) -> dict:
      """Get status."""

  # Clear -- the LLM knows exactly when and how to call this
  def get_order_status(
      order_id: Annotated[str, Field(description="The order ID, e.g. ORD-2025-12345")]
  ) -> dict:
      """Retrieve the current delivery status and tracking info for a customer order."""
  ```
- Verify tools are registered by listing the agent's configuration:
  ```python
  agent_info = client.get_agent(agent_id)
  print(f"Registered tools: {[t.function.name for t in agent_info.tools]}")
  ```

### Problem: Tool function raises an exception

**Symptoms:**
```
requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8081): Max retries exceeded
```

**Causes:** The mock backend service is not running.

**Solutions:**
- Start the Docker Compose services: `docker compose up`
- Verify the service is reachable: `curl http://localhost:8081/health`
- If running inside Docker, use service names instead of `localhost`:
  ```python
  # Inside Docker Compose network
  CRM_ENDPOINT = os.getenv("CRM_ENDPOINT", "http://mock-crm:8081")
  ```

### Problem: Tool returns unexpected format

**Symptoms:** The agent produces a confused or incorrect response after calling a tool.

**Solutions:**
- Ensure tool functions return JSON-serialisable dictionaries.
- Include enough context in the return value for the LLM to form a good response:
  ```python
  # Minimal -- the LLM has little to work with
  return {"status": "ok"}

  # Informative -- the LLM can build a natural response
  return {
      "status": "in_delivery",
      "estimated_delivery": "2025-03-19 10:00-14:00",
      "carrier": "DHL",
      "tracking_url": "https://tracking.dhl.de/...",
  }
  ```

---

## Rate Limiting and Quotas

### Problem: HTTP 429 Too Many Requests

**Symptoms:**
```
azure.core.exceptions.HttpResponseError: (429) Rate limit exceeded
```

**Solutions:**
- Implement exponential backoff:
  ```python
  import asyncio

  async def call_with_retry(func, *args, max_retries=3):
      """Call a function with exponential backoff on rate limit errors."""
      for attempt in range(max_retries):
          try:
              return await func(*args)
          except Exception as e:
              if "429" in str(e) and attempt < max_retries - 1:
                  wait_time = 2 ** attempt    # 1s, 2s, 4s
                  print(f"Rate limited, retrying in {wait_time}s...")
                  await asyncio.sleep(wait_time)
              else:
                  raise
  ```
- Check your resource's quota in the Azure portal under **Resource Management > Quotas**.
- Request a quota increase if the default is insufficient for your workload.

### Problem: Foundry Agent token limit exceeded

**Symptoms:** The agent's response is truncated or an error mentions the context window.

**Solutions:**
- Reduce the conversation history by starting a new thread periodically.
- Summarise older messages before appending them to the thread.
- Use a model with a larger context window.

---

## Docker Networking Issues

### Problem: Voice agent container cannot reach mock services

**Symptoms:**
```
ConnectionRefusedError: [Errno 111] Connection refused
```

**Solutions:**
- Ensure all services are on the same Docker Compose network (they are by default in the provided `docker-compose.yml`).
- Use service names, not `localhost`, for inter-container communication:
  ```yaml
  environment:
    - CRM_ENDPOINT=http://mock-crm:8081        # service name, not localhost
  ```
- Verify services are running: `docker compose ps`
- Check logs for crash loops: `docker compose logs mock-crm`

### Problem: Port conflicts

**Symptoms:**
```
Bind for 0.0.0.0:8081 failed: port is already allocated
```

**Solutions:**
- Find the process using the port:
  ```bash
  lsof -i :8081
  ```
- Stop the conflicting process or change the port mapping in `docker-compose.yml`:
  ```yaml
  ports:
    - "9081:8081"   # Map host port 9081 to container port 8081
  ```

---

## Logging and Debugging Tips

### Enable Verbose Logging

Set the `LOG_LEVEL` environment variable to `DEBUG` for maximum verbosity:

```dotenv
LOG_LEVEL=DEBUG
```

In your application code, configure Python's logging module:

```python
import logging
import os

# Configure logging based on environment variable
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Enable Azure SDK debug logging to see HTTP requests and responses
logging.getLogger("azure").setLevel(logging.DEBUG)
```

### Inspect WebSocket Traffic

Log every message sent and received on the WebSocket for debugging:

```python
async def debug_send(ws, message: dict):
    """Send a WebSocket message with debug logging."""
    raw = json.dumps(message)
    logging.debug(f"WS SEND: {message['type']} ({len(raw)} bytes)")
    await ws.send(raw)

async def debug_recv(ws) -> dict:
    """Receive a WebSocket message with debug logging."""
    raw = await ws.recv()
    event = json.loads(raw)
    logging.debug(f"WS RECV: {event.get('type', 'unknown')} ({len(raw)} bytes)")
    return event
```

### Test Tools in Isolation

Before running the full voice pipeline, test each tool function independently:

```python
# Quick smoke test for the CRM tool
result = identify_customer(phone_number="+4915112345678")
print(f"CRM result: {result}")
assert "customer_id" in result, "CRM tool did not return customer_id"
```

### Check Azure Resource Health

```bash
# Verify your AI Foundry resource is healthy
az resource show \
  --name "<resource-name>" \
  --resource-group "<rg>" \
  --resource-type "Microsoft.CognitiveServices/accounts" \
  --query "properties.provisioningState"

# List model deployments
az cognitiveservices account deployment list \
  --name "<resource-name>" \
  --resource-group "<rg>" \
  --output table
```

---

## Quick Reference: Error Codes

| Error | Likely Cause | Quick Fix |
|---|---|---|
| HTTP 401 | Invalid or expired credentials | `az login` or check API key |
| HTTP 403 | Missing RBAC role assignment | Assign "Cognitive Services User" role |
| HTTP 404 | Wrong endpoint URL or resource not found | Verify `AZURE_FOUNDRY_ENDPOINT` |
| HTTP 429 | Rate limit exceeded | Implement backoff, check quotas |
| WS 1008 | Policy violation (bad input) | Validate JSON and audio format |
| WS 1011 | Server error | Check Azure service health, retry |
| `ConnectionRefusedError` | Backend service not running | `docker compose up` |

---

## Further Resources

- [Azure AI Foundry Documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/)
- [Voice Live API FAQ](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-faq)
- [Azure Identity -- DefaultAzureCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential)
- [Azure Service Health Dashboard](https://status.azure.com/)
- [Azure SDK for Python -- Logging](https://learn.microsoft.com/en-us/azure/developer/python/sdk/logging)
- [Voice Live API Reference](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-api-reference)
- [Foundry Agent Service Overview](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/overview)
