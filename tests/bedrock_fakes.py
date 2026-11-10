"""AWS 접속 없이 기존 API·그래프를 검증하기 위한 Converse SDK 대역."""

from unittest.mock import MagicMock

from app.intake.bedrock_provider import TOOL_NAME


def bedrock_response(output, *, usage=True):
    response = {
        "metrics": {"latencyMs": 1},
        "stopReason": "tool_use" if output is not None else "end_turn",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tool-1",
                            "name": TOOL_NAME,
                            "input": output.model_dump(mode="json"),
                        }
                    }
                ]
                if output is not None
                else [{"text": "invalid unstructured output"}],
            }
        },
    }
    if usage:
        response["usage"] = {"inputTokens": 12, "outputTokens": 8, "totalTokens": 20}
    return response


def sdk_client(*outputs):
    client = MagicMock()
    client.converse.side_effect = [bedrock_response(output) for output in outputs]
    return client


def sdk_session(client):
    session = MagicMock()
    session.client.return_value = client
    return session
