from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from azure.identity.aio import AzureCliCredential
from agent_framework.azure import AzureAIClient

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
)
logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).resolve().parent / "data" / "orders.json"

INSTRUCTIONS = """Du bist ein professioneller Kundenservice-Agent für ein deutsches Unternehmen.
Antworte kurz, klar und in der Sie-Form (Sprache wird vorgelesen).

Bestell-Logik (strikt):
1) Wenn eine Order-ID vorhanden ist (z. B. ORD-5001): nutze die passende Bestellung.
2) Wenn eine Customer-ID vorhanden ist (z. B. C-1001): nenne die letzten Bestellungen.
3) Wenn nur eine Telefonnummer vorhanden ist: finde den Kunden und nenne die letzten Bestellungen.
4) Wenn nichts vorhanden ist: bitte kurz nach Order-ID oder Kundennummer fragen.

Gib Status, Lieferdatum/Zeitraum und die wichtigsten Artikel an.
Nutze ausschließlich die bereitgestellten DATEN.
"""


def load_dataset() -> dict:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Dataset not found: {DATA_PATH}")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def build_user_message(user_text: str, dataset: dict) -> str:
    data_json = json.dumps(dataset, ensure_ascii=False, indent=2)
    return (
        f"Nutzeranfrage: {user_text}\n\n"
        f"DATEN (JSON):\n{data_json}"
    )


async def run_agent(user_text: str) -> str:
    dataset = load_dataset()

    # Prefer dedicated Azure AI project env vars, but allow fallbacks
    project_endpoint = (
        os.getenv("AZURE_AI_PROJECT_ENDPOINT")
        or os.getenv("AZURE_EXISTING_AIPROJECT_ENDPOINT")
    )
    model_deployment = (
        os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        or os.getenv("MODEL_DEPLOYMENT_NAME")
    )

    if not project_endpoint:
        raise RuntimeError(
            "AZURE_AI_PROJECT_ENDPOINT is not set (or AZURE_EXISTING_AIPROJECT_ENDPOINT)."
        )
    if not model_deployment:
        raise RuntimeError(
            "AZURE_AI_MODEL_DEPLOYMENT_NAME is not set (or MODEL_DEPLOYMENT_NAME)."
        )

    async with AzureCliCredential() as credential:
    client = AzureAIClient(
        credential=credential,
        project_endpoint=project_endpoint,
        model_deployment_name=model_deployment,
    )
        agent = client.as_agent(instructions=INSTRUCTIONS)

        message = build_user_message(user_text, dataset)
        logger.info("Sending user query to agent")
        result = await agent.run(message)
        return getattr(result, "text", str(result))


def main() -> None:
    if len(sys.argv) > 1:
        user_text = " ".join(sys.argv[1:]).strip()
    else:
        user_text = "Wo ist meine Bestellung ORD-5001?"

    try:
        response = asyncio.run(run_agent(user_text))
    except Exception as exc:
        logger.error("Agent call failed: %s", exc)
        raise

    print("\n=== AGENT RESPONSE ===")
    print(response)


if __name__ == "__main__":
    main()
