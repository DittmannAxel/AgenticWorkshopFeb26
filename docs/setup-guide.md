# Setup Guide

## Introduction

This guide walks you through every step required to run the Azure AI Foundry Voice Agent Demo -- from provisioning Azure resources to starting the application locally. By the end you will have a working voice agent that accepts spoken German input, reasons over it with a Foundry Agent, calls backend tools, and responds with synthesised speech.

If you are new to Azure AI Foundry, read the [architecture document](architecture.md) first so you understand what each component does and why it is needed.

---

## Prerequisites

Before you begin, make sure you have the following:

| Requirement | Minimum Version | Notes |
|---|---|---|
| **Azure subscription** | -- | A pay-as-you-go or Enterprise subscription with permission to create resources. [Create a free account](https://azure.microsoft.com/free/) if needed. |
| **Python** | 3.10+ | Required by the project (`pyproject.toml` sets `requires-python = ">=3.10"`). |
| **Azure CLI** | 2.60+ | Used for authentication and resource provisioning. [Install the Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli). |
| **Docker & Docker Compose** | 24+ / 2.20+ | Only needed if you want to run the mock backend services via Docker. |
| **Git** | 2.x | To clone the repository. |

---

## Step 1: Clone the Repository

```bash
git clone https://github.com/<your-org>/azure-foundry-voice-agent-demo.git
cd azure-foundry-voice-agent-demo
```

---

## Step 2: Create an Azure AI Foundry Resource

An AI Foundry resource is the top-level container for your agent, model deployments, and connected Azure AI services (including Speech).

```bash
# Log in to Azure
az login

# Create a resource group (choose a region that supports Voice Live API)
az group create \
  --name rg-voice-agent-demo \
  --location eastus2

# Create the AI Foundry resource
# (The portal wizard is recommended for first-time setup because it
#  creates the linked Speech, OpenAI, and Storage resources automatically.)
```

> **Tip:** The portal-based setup is described in the [Azure AI Foundry quickstart](https://learn.microsoft.com/en-us/azure/ai-foundry/quickstarts/get-started-code). It provisions everything with a single click.

After creation, note down the **endpoint** -- it looks like this:

```
https://<resource-name>.services.ai.azure.com
```

You will also need the **project name** visible in the AI Foundry portal.

---

## Step 3: Deploy a Model

Inside the AI Foundry portal, go to **Model Catalog** and deploy a model such as `gpt-4.1`. Note the **deployment name** -- you will use it in the `.env` file.

---

## Step 4: Configure the Speech Service / Voice Live API

Voice Live is part of the Speech Service linked to your AI Foundry resource. Ensure the linked Speech resource is in a [region that supports Voice Live](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live).

No additional configuration is needed beyond the AI Foundry resource -- the Voice Live WebSocket endpoint is derived from the same base URL:

```
wss://<resource-name>.services.ai.azure.com/voice-live/realtime?api-version=2025-10-01
```

> **Reference:** [Voice Live API Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart)

---

## Step 5: Create a Foundry Agent with Tools

You can create the agent programmatically (see [agent-service.md](agent-service.md)) or via the AI Foundry portal. The agent needs:

- A **model deployment** (the one you created in Step 3).
- **System instructions** describing the persona and language.
- **Tool definitions** (the Python functions in `src/tools/`).

```python
# Minimal example -- see agent-service.md for the full version
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

# Authenticate using Azure CLI credentials during development
credential = DefaultAzureCredential()

# Create the client pointing to your AI Foundry project
client = AgentsClient(
    endpoint="https://<resource>.services.ai.azure.com/api/projects/<project>",
    credential=credential,
)

# Create the agent with a German customer-service persona
agent = client.create_agent(
    model="gpt-4.1",                   # must match your deployment name
    name="customer-service-agent",
    instructions=(
        "Du bist ein freundlicher deutschsprachiger Kundenservice-Agent. "
        "Antworte immer auf Deutsch und nutze die bereitgestellten Tools, "
        "um Kundenanfragen zu beantworten."
    ),
    tools=tool_definitions,             # list of FunctionTool objects
)
```

> **Reference:** [Foundry Agent Quickstart](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/quickstart)

---

## Step 6: Configure Environment Variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` -- the variables must match exactly what the application reads:

```dotenv
# Azure AI Foundry endpoint (required, no trailing slash)
# Find it in the AI Foundry portal under your resource > Overview
AZURE_FOUNDRY_ENDPOINT=https://<resource-name>.services.ai.azure.com

# Your project name from the AI Foundry portal (required)
PROJECT_NAME=<project>

# The model deployment name from Step 3 (required)
MODEL_DEPLOYMENT_NAME=gpt-4.1

# The neural voice for TTS output (optional, default: de-DE-ConradNeural)
# Browse all voices: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts
VOICE_LIVE_VOICE=de-DE-ConradNeural

# Voice Live API version (optional, default: 2025-10-01)
VOICE_LIVE_API_VERSION=2025-10-01

# Authentication: DefaultAzureCredential is used by default.
# It picks up Azure CLI credentials locally and Managed Identity in production.
# Uncomment the next line ONLY if you need API key auth instead:
# AZURE_API_KEY=<your-api-key>

# Logging verbosity (optional, default: INFO)
LOG_LEVEL=INFO
```

> **Important:** Never commit `.env` to source control. It is already listed in `.gitignore`.
>
> **Note:** The Voice Live WebSocket URL is derived automatically from `AZURE_FOUNDRY_ENDPOINT`:
> `wss://<resource>.services.ai.azure.com/voice-live/realtime?api-version=2025-10-01&model=<MODEL_DEPLOYMENT_NAME>`

---

## Step 7: Install Dependencies and Run Locally

```bash
# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate

# Install the project and its dependencies
pip install -e ".[dev]"

# Make sure you are logged in to Azure (for DefaultAzureCredential)
az login

# Run the demo (adjust the entry point to the example you want)
python -m examples.01_voice_live_basic
```

The application will:

1. Establish a WebSocket connection to the Voice Live API.
2. Create (or reuse) a Foundry Agent.
3. Begin listening for audio input and responding with synthesised speech.

---

## Step 8 (Alternative): Docker Setup

If you prefer containers, or if you want to run the mock backend services:

```bash
# Build and start all services (mock backends + voice agent)
docker compose up --build

# The mock services will be available at:
#   CRM:      http://localhost:8081
#   Calendar: http://localhost:8082
#   Orders:   http://localhost:8083
#   Tickets:  http://localhost:8084
```

The `docker-compose.yml` defines five services:

| Service | Port | Purpose |
|---|---|---|
| `mock-crm` | 8081 | Simulated CRM backend |
| `mock-calendar` | 8082 | Simulated calendar backend |
| `mock-orders` | 8083 | Simulated order management backend |
| `mock-tickets` | 8084 | Simulated ticket system backend |
| `voice-agent` | -- | The main voice agent application |

The `voice-agent` service reads your `.env` file and connects to the mock backends via Docker networking (using service names as hostnames).

---

## Verifying the Setup

Once the application is running, you can test it by speaking into your microphone (if using a WebRTC client) or sending audio via the WebSocket. A successful interaction looks like this:

```
Kunde: "Hallo, ich moechte einen Termin buchen."
Agent: "Guten Tag! Fuer welchen Tag moechten Sie den Termin?"
Kunde: "Naechsten Dienstag bitte."
Agent: "Dienstag um 10 Uhr ist frei. Soll ich den Termin fuer Sie buchen?"
```

If you see errors instead, consult the [troubleshooting guide](troubleshooting.md).

---

## Troubleshooting Hints

- **`az login` fails or token expired:** Run `az login` again and ensure your subscription is selected with `az account set --subscription <id>`.
- **`pip install` fails on `azure-ai-voicelive`:** This package may require a specific index URL. Check the [Azure SDK for Python releases](https://learn.microsoft.com/en-us/python/api/overview/azure/) for the latest instructions.
- **Docker build fails:** Make sure Docker Desktop is running. The repository includes `Dockerfile` (voice agent) and `Dockerfile.mock` (backend services) in the project root.
- **Connection refused to mock services:** Ensure Docker Compose is running and the ports are not occupied by other processes.

---

## Further Resources

- [Azure AI Foundry -- Getting Started with Code](https://learn.microsoft.com/en-us/azure/ai-foundry/quickstarts/get-started-code)
- [Voice Live API Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-quickstart)
- [Foundry Agent Quickstart](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/quickstart)
- [Azure CLI Installation](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
- [Azure Identity -- DefaultAzureCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential)
