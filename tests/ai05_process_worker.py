"""AI 프로세스 재기동 검사 전용. SDK 대역을 쓰며 실제 AWS에는 접속하지 않는다."""

import json
import os
import sys
from unittest.mock import patch

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.intake.providers import ModelOutput
from app.main import create_app
from tests.bedrock_fakes import sdk_client, sdk_session


def main() -> None:
    data = json.load(sys.stdin)
    Settings.model_config["env_file"] = None
    for name in Settings.model_fields:
        os.environ.pop(name.upper(), None)
    os.environ["INTERNAL_KEY"] = "process-test-only"
    os.environ["BEDROCK_MODEL_ID"] = "mock-process-model"
    get_settings.cache_clear()
    client = sdk_client(ModelOutput.model_validate(data.get("output", {})))
    if data.get("error"):
        client.converse.side_effect = ClientError(
            {"Error": {"Code": data["error"], "Message": "private-provider-detail"}}, "Converse"
        )
    with (
        patch("boto3.session.Session", return_value=sdk_session(client)),
        TestClient(create_app()) as api,
    ):
        response = api.post(
            "/internal/v1/inquiries/analyze",
            json=data["request"],
            headers={"X-Internal-Key": "process-test-only"},
        )
    json.dump(
        {
            "pid": os.getpid(),
            "status": response.status_code,
            "body": response.json(),
            "modelCalls": client.converse.call_count,
        },
        sys.stdout,
        ensure_ascii=True,
    )


if __name__ == "__main__":
    main()
