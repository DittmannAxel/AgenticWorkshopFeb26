# Foundry Agent Service

## Introduction

The Azure AI Foundry Agent Service provides a managed runtime for building AI agents that can hold multi-turn conversations, call external tools, and maintain state across interactions. Instead of orchestrating raw LLM calls, tool invocations, and conversation history yourself, you define an agent once and let the service handle the rest.

This document explains the concepts behind the Agent Service, shows how to create and configure agents with the Python SDK, demonstrates tool definitions using Pydantic type hints, and illustrates how the agent integrates with the Voice Live API for voice-based customer service.

> **Official documentation:** [Foundry Agent Service Overview](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/overview)

---

## What is Foundry Agent Service?

An *agent* in Azure AI Foundry is a persistent, stateful entity that wraps:

- **A model deployment** -- the LLM (e.g., `gpt-4.1`) that powers the agent's reasoning.
- **System instructions** -- the persona, behavioural rules, and domain context the model should follow.
- **Tool definitions** -- functions the model can call to retrieve data or perform actions.
- **Threads** -- conversation histories that persist across turns, so the agent remembers what was said earlier.

The key benefit is that the service manages the tool-calling loop internally: when the model decides to call a tool, the service pauses generation, invokes your tool function, feeds the result back to the model, and continues generating -- all in a single API call from your perspective.

> **Workshop:** [Build Your First Agent with Azure AI Agent Service](https://microsoft.github.io/build-your-first-agent-with-azure-ai-agent-service-workshop/)

---

## Creating Agents with the Python SDK

### Installation

There are two Python packages for working with Foundry Agents:

```bash
# Option A: Standalone agents client (used in this project)
pip install azure-ai-agents azure-identity

# Option B: Higher-level project client (wraps agents + more)
pip install azure-ai-projects azure-identity
```

This project uses `azure-ai-agents` (Option A) for direct agent control. The quickstart tutorials on the Azure docs use `azure-ai-projects` (Option B). Both work; choose based on your needs.

### Client Setup

```python
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

# DefaultAzureCredential automatically uses:
# - Azure CLI credentials during local development (run `az login` first)
# - Managed Identity in production (Azure App Service, AKS, etc.)
credential = DefaultAzureCredential()

# The endpoint includes the project path.
# Format: https://<resource>.services.ai.azure.com/api/projects/<project>
client = AgentsClient(
    endpoint="https://my-foundry.services.ai.azure.com/api/projects/my-project",
    credential=credential,
)
```

**Alternative with AIProjectClient:**

```python
# If using azure-ai-projects instead
from azure.ai.projects import AIProjectClient

project = AIProjectClient(
    endpoint="https://my-foundry.services.ai.azure.com/api/projects/my-project",
    credential=DefaultAzureCredential(),
)
# Access agents via project.agents.*
agent = project.agents.create_agent(model="gpt-4.1", ...)
```

> **Reference:** [SDK Overview](https://learn.microsoft.com/en-us/azure/ai-foundry/how-to/develop/sdk-overview)

### Creating an Agent

```python
from azure.ai.agents.models import FunctionTool

# Define the agent with its persona, model, and tools.
# The instructions are in German because this agent serves German-speaking customers.
agent = client.create_agent(
    model="gpt-4.1",                           # Must match your deployment name
    name="customer-service-agent-de",
    instructions="""Du bist ein professioneller und freundlicher Kundenservice-Agent.

Regeln:
- Antworte immer auf Deutsch.
- Nutze die bereitgestellten Tools, um Kundenanfragen zu bearbeiten.
- Frage nach, wenn Informationen fehlen, bevor du ein Tool aufrufst.
- Fasse die Ergebnisse verstaendlich fuer den Kunden zusammen.
- Sei hoeflich und empathisch, besonders bei Beschwerden.
""",
    tools=tool_definitions,                     # List of FunctionTool objects (see below)
)

print(f"Agent created: {agent.id}")
```

---

## Agent Configuration

The `create_agent` call accepts several parameters that control the agent's behaviour:

| Parameter | Type | Description |
|---|---|---|
| `model` | `str` | The name of the model deployment in your AI Foundry project. |
| `name` | `str` | A human-readable name for the agent (useful when managing multiple agents). |
| `instructions` | `str` | System-level instructions the model follows throughout every conversation. |
| `tools` | `list[ToolDefinition]` | The tools (functions) the agent can invoke. |
| `temperature` | `float` | Controls randomness in responses (0.0 = deterministic, 1.0 = creative). Default varies by model. |
| `top_p` | `float` | Nucleus sampling parameter. |
| `metadata` | `dict` | Arbitrary key-value pairs for tagging and filtering agents. |

---

## Tool Definitions with Pydantic and Type Hints

Tools are the mechanism through which the agent interacts with external systems. You define tools as plain Python functions. The SDK inspects the function signature, docstring, and type annotations to generate a JSON schema that the LLM understands.

### Pattern

```python
from typing import Annotated
from pydantic import Field

def tool_function(
    param_name: Annotated[str, Field(description="What this parameter is for")]
) -> dict:
    """
    Short description of what this tool does.
    The LLM reads this docstring to decide when to call the tool.
    """
    # Implementation: call your backend, database, or API
    return {"result": "value"}
```

### CRM Tool Example

```python
def identify_customer(
    phone_number: Annotated[str, Field(description="The customer's phone number in E.164 format, e.g. +4915112345678")]
) -> dict:
    """
    Look up a customer in the CRM system by their phone number.
    Returns the customer's name, account ID, and account status.
    """
    # In production this would call the real CRM API.
    # During development we call the mock service running in Docker.
    import requests
    response = requests.get(
        f"http://localhost:8081/customers",
        params={"phone": phone_number},
    )
    return response.json()
```

### Calendar Tool Example

```python
def check_availability(
    date: Annotated[str, Field(description="The date to check in ISO 8601 format, e.g. 2025-03-15")],
    service_type: Annotated[str, Field(description="Type of appointment: 'beratung', 'reparatur', or 'installation'")]
) -> dict:
    """
    Check available time slots on the given date for the specified service type.
    Returns a list of available slots with start and end times.
    """
    import requests
    response = requests.get(
        f"http://localhost:8082/availability",
        params={"date": date, "service_type": service_type},
    )
    return response.json()


def book_appointment(
    customer_id: Annotated[str, Field(description="The customer's account ID")],
    date: Annotated[str, Field(description="Appointment date in ISO 8601 format")],
    time_slot: Annotated[str, Field(description="The selected time slot, e.g. '10:00-11:00'")],
    service_type: Annotated[str, Field(description="Type of appointment")]
) -> dict:
    """
    Book an appointment for the customer on the specified date and time slot.
    Returns a confirmation with the booking reference number.
    """
    import requests
    response = requests.post(
        f"http://localhost:8082/appointments",
        json={
            "customer_id": customer_id,
            "date": date,
            "time_slot": time_slot,
            "service_type": service_type,
        },
    )
    return response.json()
```

### Order Tool Example

```python
def get_order_status(
    order_id: Annotated[str, Field(description="The order ID, e.g. ORD-2025-12345")]
) -> dict:
    """
    Retrieve the current status and delivery estimate for the given order.
    Returns status, expected delivery date, and tracking information.
    """
    import requests
    response = requests.get(f"http://localhost:8083/orders/{order_id}")
    return response.json()
```

### Ticket Tool Example

```python
def create_ticket(
    customer_id: Annotated[str, Field(description="The customer's account ID")],
    description: Annotated[str, Field(description="Description of the customer's issue")],
    priority: Annotated[str, Field(description="Ticket priority: 'low', 'medium', or 'high'")]
) -> dict:
    """
    Create a support ticket for the customer's issue.
    Returns the ticket ID and estimated response time.
    """
    import requests
    response = requests.post(
        f"http://localhost:8084/tickets",
        json={
            "customer_id": customer_id,
            "description": description,
            "priority": priority,
        },
    )
    return response.json()
```

### Registering Tools

To register these functions as agent tools, use `FunctionTool` and `ToolSet`:

```python
from azure.ai.agents import FunctionTool, ToolSet

# Collect all tool functions in a list
user_functions = [
    identify_customer,
    check_availability,
    book_appointment,
    get_order_status,
    create_ticket,
]

# FunctionTool wraps them for the agent. The SDK extracts name,
# description, and parameter schema from function signatures and docstrings.
functions = FunctionTool(user_functions)

# ToolSet enables auto function calling: when the agent decides
# to call a tool, the SDK executes it automatically.
toolset = ToolSet()
toolset.add(functions)

# Enable auto function calls on the client
client.enable_auto_function_calls(toolset)

# Pass the toolset when creating the agent
agent = client.create_agent(
    model="gpt-4.1",
    name="customer-service-agent",
    instructions="...",
    toolset=toolset,
)
```

> **Note:** Without `enable_auto_function_calls`, you would need to handle
> `requires_action` run status and execute tools manually. The ToolSet approach
> is much simpler for most use cases.

---

## Running Agent Conversations

Conversations are managed through *threads*. A thread is a persistent message history that the agent refers to when generating responses.

```python
# Create a new conversation thread
thread = client.threads.create()

# Add a user message (in a voice scenario this comes from STT)
client.messages.create(
    thread_id=thread.id,
    role="user",
    content="Ich moechte den Status meiner Bestellung ORD-2025-12345 wissen.",
)

# Run the agent on the thread.
# The agent will:
#   1. Read the user message
#   2. Decide to call get_order_status(order_id="ORD-2025-12345")
#   3. Receive the tool result
#   4. Generate a natural-language response
run = client.runs.create_and_process(
    thread_id=thread.id,
    agent_id=agent.id,
)

# Retrieve the agent's response
messages = client.messages.list(thread_id=thread.id)
for msg in messages:
    if msg.role == "assistant":
        print(msg.content)
```

### Example Output (German)

```
Ihre Bestellung ORD-2025-12345 ist derzeit in Zustellung.
Die voraussichtliche Lieferung ist morgen, Mittwoch, zwischen 10:00 und 14:00 Uhr.
Sie koennen den Sendungsverlauf unter dem Tracking-Link verfolgen, den ich Ihnen
gerne per SMS zusenden kann.
```

---

## Integrating with Voice Live API

When combining the Agent Service with Voice Live, the Voice Live API handles audio I/O while the Agent Service handles reasoning and tool calling. The integration flow is:

1. **Voice Live** transcribes the customer's speech to text.
2. Your application sends the transcribed text to the **Agent Service** as a user message.
3. The Agent Service processes the message, potentially calling tools.
4. The agent's text response is sent back to **Voice Live** for TTS synthesis.

```python
async def handle_voice_turn(
    transcribed_text: str,
    client: AgentsClient,
    thread_id: str,
    agent_id: str,
) -> str:
    """
    Process one voice turn: take the STT output, run the agent,
    and return the agent's text response for TTS synthesis.
    """
    # Add the transcribed speech as a user message
    client.messages.create(
        thread_id=thread_id,
        role="user",
        content=transcribed_text,
    )

    # Run the agent -- it handles tool calls internally
    run = client.runs.create_and_process(
        thread_id=thread_id,
        agent_id=agent_id,
    )

    # Extract the latest assistant message
    messages = client.messages.list(thread_id=thread_id)
    for msg in reversed(list(messages)):
        if msg.role == "assistant":
            return msg.content

    return "Entschuldigung, ich konnte Ihre Anfrage leider nicht verarbeiten."
```

> **Reference:** [Voice Live Agent Integration Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart)

---

## Example Scenario: Full Customer Service Dialogue (German)

```
Kunde:   "Hallo, ich moechte einen Termin fuer eine Reparatur buchen."
Agent:   → identify_customer(phone_number="+4915112345678")
         ← {"customer_id": "C-1234", "name": "Max Mustermann", "status": "active"}
Agent:   "Guten Tag, Herr Mustermann! Fuer welchen Tag moechten Sie den
          Reparaturtermin?"

Kunde:   "Geht naechsten Dienstag?"
Agent:   → check_availability(date="2025-03-18", service_type="reparatur")
         ← {"slots": ["09:00-10:00", "10:00-11:00", "14:00-15:00"]}
Agent:   "Am Dienstag, dem 18. Maerz, sind folgende Zeiten frei:
          9 bis 10 Uhr, 10 bis 11 Uhr, und 14 bis 15 Uhr.
          Welcher Termin passt Ihnen am besten?"

Kunde:   "10 Uhr bitte."
Agent:   → book_appointment(customer_id="C-1234", date="2025-03-18",
              time_slot="10:00-11:00", service_type="reparatur")
         ← {"booking_ref": "B-5678", "confirmed": true}
Agent:   "Perfekt, ich habe den Termin fuer Dienstag, 18. Maerz, von 10 bis 11 Uhr
          gebucht. Ihre Buchungsnummer ist B-5678. Kann ich sonst noch etwas
          fuer Sie tun?"

Kunde:   "Nein, das war's. Danke!"
Agent:   "Sehr gerne, Herr Mustermann. Ich wuensche Ihnen einen schoenen Tag!"
```

---

## Troubleshooting Hints

- **Agent does not use tools:** The LLM decides whether to call a tool based on the tool's docstring and parameter descriptions. Make sure these are clear, specific, and written in a way that matches the user's likely phrasing.
- **Tool call fails with serialisation error:** Ensure all tool parameters use supported types (`str`, `int`, `float`, `bool`, `list`, `dict`) and are annotated with `Annotated[..., Field(...)]`.
- **`create_and_process` times out:** The agent may be waiting for a tool result. Check that your tool functions return within a reasonable time. Set timeouts on HTTP calls to backend services.
- **Thread grows too large:** For long-running sessions (e.g., multi-hour support calls), consider summarising older messages or starting a new thread with a summary.
- **Wrong model deployment name:** The `model` parameter in `create_agent` must exactly match the deployment name in your AI Foundry project, not the base model name.

---

## Further Resources

- [Foundry Agent Service Overview](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/overview)
- [Agent Service Quickstart](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/quickstart)
- [SDK Overview](https://learn.microsoft.com/en-us/azure/ai-foundry/how-to/develop/sdk-overview)
- [Python SDK Reference (azure-ai-agents)](https://learn.microsoft.com/en-us/python/api/overview/azure/ai-agents-readme)
- [Tools Overview](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools-classic/overview)
- [Agent Types](https://learn.microsoft.com/en-us/agent-framework/user-guide/agents/agent-types/)
- [Build Your First Agent Workshop](https://microsoft.github.io/build-your-first-agent-with-azure-ai-agent-service-workshop/)
