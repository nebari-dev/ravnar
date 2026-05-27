import pytest

from _ravnar import schema
from _ravnar.config import BaseConfig
from tests.utils import make_app_client


@pytest.mark.parametrize("storage_enabled", [True, False])
def test_storage_enabled(storage_enabled):
    with make_app_client(config=BaseConfig.model_validate({"storage": {"enabled": storage_enabled}})) as client:
        response = client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.storage_enabled is storage_enabled
