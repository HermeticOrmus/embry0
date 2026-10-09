import os
from unittest.mock import patch

from embry0.config import Embry0Config


def test_quarantine_defaults():
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None)
    assert cfg.credential_rate_limit_quarantine_seconds == 900
    assert cfg.credential_quota_quarantine_seconds == 3600


def test_quarantine_env_override():
    with patch.dict(os.environ, {"CREDENTIAL_QUOTA_QUARANTINE_SECONDS": "60"}, clear=True):
        assert Embry0Config(_env_file=None).credential_quota_quarantine_seconds == 60
