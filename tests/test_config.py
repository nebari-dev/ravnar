import json
import os
from copy import deepcopy

import pytest
import yaml
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, YamlConfigSettingsSource

from _ravnar.agents import Agent
from _ravnar.config import BaseConfig, Config


@pytest.fixture()
def make_test_config(mocker, tmp_path):
    env_prefix = "TEST_RAVNAR_"
    yaml_file = tmp_path / "config.yml"

    def factory(file=None, *, env=None, env_json=None):
        mock_model_config = deepcopy(Config.model_config)
        if env or env_json:
            if env and env_json:
                raise ValueError("env and env_json cannot both be set")

            mock_model_config["env_prefix"] = env_prefix

            def set_as_env_var(value, *, key=()):
                if isinstance(value, dict):
                    if env_json and len(value) == 1:
                        k, v = next(iter(value.items()))
                        set_as_env_var(json.dumps(v), key=[*key, k])
                    else:
                        for k, v in value.items():
                            set_as_env_var(v, key=[*key, k])
                elif isinstance(value, list):
                    assert env_json is None
                    raise TypeError("list values are only supported for env_json")
                else:
                    k = env_prefix + mock_model_config["env_nested_delimiter"].join(k.upper() for k in key)
                    v = str(value)
                    print(f"{k}={v!r}")
                    mocker.patch.dict(
                        os.environ,
                        {k: v},
                    )

            set_as_env_var(env or env_json)

        if file:
            mock_model_config["yaml_file"] = yaml_file

            with open(yaml_file, "w") as f:
                yaml.dump(file, f)

        class MockConfig(BaseConfig):
            model_config = mock_model_config

            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                sources = []
                if env or env_json:
                    sources.append(env_settings)
                if file:
                    sources.append(YamlConfigSettingsSource(settings_cls))
                return tuple(sources)

        return MockConfig.model_validate({})

    return factory


def new_test_config_cls(env_prefix=None, yaml_file=None):
    test_model_config = deepcopy(Config.model_config)
    if env_prefix is not None:
        test_model_config["env_prefix"] = env_prefix
    if yaml_file is not None:
        test_model_config["yaml_file"] = yaml_file

    class TestConfig(BaseConfig):
        model_config = test_model_config

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            sources = [init_settings]
            if env_prefix is not None:
                sources.append(env_settings)
            if yaml_file is not None:
                sources.append(YamlConfigSettingsSource(settings_cls))
            return tuple(sources)

    return TestConfig


@pytest.fixture
def create_config_file(tmp_path):
    def factory(config):
        p = tmp_path / "config.yml"
        with open(p, "w") as f:
            yaml.dump(config, f)
        return p

    return factory


def test_file_gt_defaults(make_test_config):
    port = 80

    config = make_test_config(file={"server": {"port": port}})

    assert config.server.port == port


def test_env_gt_file(make_test_config):
    port_env = 80
    port_file = 8080

    config = make_test_config(file={"server": {"port": port_file}}, env={"server": {"port": port_env}})

    assert config.server.port == port_env


def test_file_and_env(make_test_config):
    hostname = "0.0.0.0"
    port = 80

    config = make_test_config(file={"server": {"port": port}}, env={"server": {"hostname": hostname}})

    assert config.server.hostname == hostname
    assert config.server.port == port


@pytest.mark.parametrize("source", ["file", "env", "env_json"])
def test_template_rendering(mocker, make_test_config, source):
    templated_port = 80
    template = "{{ TEMPLATED_PORT | int + 1 }}"

    mocker.patch.dict(os.environ, {"TEMPLATED_PORT": str(templated_port)})

    config = make_test_config(**{source: {"server": {"port": template}}})

    assert config.server.port == templated_port + 1


@pytest.mark.parametrize("source", ["file", "env_json"])
def test_template_rendering_in_list(mocker, make_test_config, source):
    app_domain = "example.com"
    template = "https://{{ APP_DOMAIN }}"

    mocker.patch.dict(os.environ, {"APP_DOMAIN": app_domain})

    config = make_test_config(**{source: {"security": {"cors": {"allowed_origins": [template]}}}})

    assert config.security.cors.allowed_origins == [f"https://{app_domain}"]


class MockAgent(Agent):
    def __init__(self, param="unset"):
        self.param = param

    async def run(self, input):
        raise AssertionError
        yield


@pytest.mark.parametrize("source", ["file", "env", "env_json"])
@pytest.mark.parametrize("input_type", ["plain", "object"])
def test_import_string_with_params(make_test_config, source, input_type):
    import_path = f"{__name__}.{MockAgent.__name__}"
    default_param = "unset"
    explicit_param = "sentinel"

    match input_type:
        case "plain":
            value = import_path
            expected_param = default_param
        case "object":
            value = json.dumps({"cls_or_fn": import_path, "params": {"param": explicit_param}})
            expected_param = explicit_param
        case _:
            raise ValueError(f"unknown {input_type=}")

    id = "mock"
    config = make_test_config(**{source: {"agents": {id: value}}})

    instance = config.agents[id]()

    assert isinstance(instance, MockAgent)
    assert instance.param == expected_param
