# Example 05: Local Agent Framework (Simple Order Lookup)

This example uses the **Microsoft Agent Framework** locally to answer order
questions based on a small JSON dataset. It does **not** call the Azure AI
Agent Service. Instead, it uses the Agent Framework SDK directly with your
Azure AI project.

## What it does

- Loads a small local dataset (`data/orders.json`)
- Sends a user query to an Agent Framework agent
- The agent answers using the dataset (order ID or customer ID)

## Setup

```bash
cd examples/05_agent_framework_local

# Install the Agent Framework SDK (pre-release)
pip install agent-framework --pre

# Authenticate
az login
```

## Required environment variables

```bash
# Azure AI project endpoint (Foundry)
export AZURE_AI_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"

# Model deployment name
export AZURE_AI_MODEL_DEPLOYMENT_NAME="gpt-4.1"
```

You can also use these fallbacks:

- `AZURE_EXISTING_AIPROJECT_ENDPOINT` (instead of AZURE_AI_PROJECT_ENDPOINT)
- `MODEL_DEPLOYMENT_NAME` (instead of AZURE_AI_MODEL_DEPLOYMENT_NAME)

## Run

```bash
# Default query
python main.py

# Custom query
python main.py "Wo ist meine Bestellung ORD-5001?"
python main.py "Ich bin Kunde C-1001. Welche Bestellungen habe ich?"
```

## Notes

- The agent follows strict order logic: order ID → order status; customer ID → recent orders.
- Data is intentionally small and local for clarity.
- If you want tools, you can add them later via Agent Framework tool hooks.
