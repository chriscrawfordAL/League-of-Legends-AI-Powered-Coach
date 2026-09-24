"""Chatbot backend — queries the Multi-Agent Supervisor (MAS) serving endpoint.

The MAS (Agent Bricks) routes a natural-language question to a per-table Genie
agent over fevm_central1.league_ai_coach. The endpoint speaks the OpenAI
*responses* API: request {"input": [{role, content}, ...]}, response
{"output": [ {type: "message", content: [{type: "output_text", text}]},
{type: "function_call", ...}, ... ]}. Degrades to a friendly message if the
endpoint is unset or unavailable so the chat never hard-fails the app.
"""

from __future__ import annotations

import os

# System framing so the assistant stays on-topic for this app's data.
_SYSTEM = (
    "You are the League Data Assistant for this coaching app. Answer questions "
    "about the League of Legends data using the connected Genie agents (raw "
    "matches, per-participant rows, player ranks, and tier/role benchmarks). Be "
    "concise and state the numbers you find."
)


def _endpoint() -> str:
    return os.environ.get("MAS_ENDPOINT", "")


def enabled() -> bool:
    return bool(_endpoint())


def _final_text(resp) -> str:
    """Extract the assistant's final answer from a responses-API payload.

    ``output`` is a list of items; ``message`` items carry content blocks of
    {type: "output_text", text}. The last assistant message is the synthesized
    answer (earlier ones are routing notes / raw Genie results)."""
    if not isinstance(resp, dict):
        return str(resp)
    texts = []
    for item in resp.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            block = " ".join(
                c.get("text", "") for c in (item.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "output_text")
            if block.strip():
                texts.append(block.strip())
    if texts:
        return texts[-1]
    if resp.get("error"):
        return f"The data assistant hit an error: {resp['error']}"
    return "I couldn't find an answer for that."


def ask_mas(messages: list[dict]) -> str:
    """Send the running chat history to the MAS endpoint; return assistant text.

    ``messages`` is a list of {"role": "user"|"assistant", "content": str}.
    """
    ep = _endpoint()
    if not ep:
        return "The data assistant isn't configured yet."
    inp = [{"role": "system", "content": _SYSTEM}] + [
        {"role": m.get("role", "user"), "content": m.get("content", "")}
        for m in messages if m.get("content")]
    try:
        from databricks.sdk import WorkspaceClient

        resp = WorkspaceClient().api_client.do(
            "POST", f"/serving-endpoints/{ep}/invocations", body={"input": inp})
        return _final_text(resp)
    except Exception as exc:  # noqa: BLE001
        return f"Sorry — the data assistant couldn't answer that right now ({exc})."
