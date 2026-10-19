from __future__ import annotations

from typing import Any

from oxypuro.config import boto3_session, load_analysis, load_settings


class BedrockError(RuntimeError):
    pass


def converse(system: str, user_text: str, model_id: str | None = None) -> str:
    cfg = load_analysis()["llm"]
    model_id = model_id or cfg["default_model_id"]
    client = boto3_session(load_settings()).client("bedrock-runtime")
    try:
        resp: dict[str, Any] = client.converse(
            modelId=model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={
                "temperature": cfg["temperature"],
                "maxTokens": cfg["max_tokens_brief"],
            },
        )
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
        raise BedrockError(f"bedrock-runtime:Converse on {model_id} failed: {code}: {e}") from e
    blocks = resp["output"]["message"]["content"]
    text = "".join(b["text"] for b in blocks if "text" in b).strip()
    if resp.get("stopReason") == "max_tokens":
        raise BedrockError(f"{model_id} hit the token limit before finishing (max_tokens_brief)")
    if not text:
        raise BedrockError(f"{model_id} returned no text")
    return text
