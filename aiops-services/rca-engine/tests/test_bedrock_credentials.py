from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "aiops-services" / "rca-engine"))

from app.bedrock_client import BedrockRCAClient  # noqa: E402
from app.config import settings  # noqa: E402


def test_bedrock_accepts_standard_aws_credential_chain() -> None:
    original = settings.force_rule_based
    settings.force_rule_based = False
    try:
        with patch("app.bedrock_client.boto3.Session") as session:
            session.return_value.get_credentials.return_value = object()
            client = BedrockRCAClient()
            assert client.configured is True
            session.assert_called_once_with(region_name=settings.aws_default_region)
    finally:
        settings.force_rule_based = original
