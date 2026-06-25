import json
import textwrap
import xml.etree.ElementTree
from pathlib import Path
from typing import Any

import pydantic
import pygments.lexers
import pygments.util
import yaml
from markupsafe import Markup
from mkdocs_autorefs import AutorefsInlineProcessor
from mkdocs_macros.plugin import MacrosPlugin
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from _ravnar.config import Config


class PrettyYAMLDumper(yaml.Dumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def code(content: str, *, language: str = "", level: int = 3):
    delimiter = "`" * level
    return "\n".join(
        [
            f"{delimiter}{language}",
            content,
            delimiter,
        ]
    )


class ConfigOptionsRenderer:
    def __init__(self):
        class DefaultConfig(Config):
            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                return (init_settings,)

        self._default_config = DefaultConfig.model_validate({})

        self.PrettyYAMLDumper = PrettyYAMLDumper

    def _config_file(self, attrs: list[str], value: Any) -> str:
        data = value
        for a in reversed(attrs):
            data = {a: data}
        return yaml.dump(data, Dumper=PrettyYAMLDumper, sort_keys=False).strip()

    def _env_var(self, attrs: list[str], value: Any) -> str:
        if isinstance(value, (list, dict)):
            value = json.dumps(value)
        return "".join(
            [
                self._default_config.model_config["env_prefix"],
                self._default_config.model_config["env_nested_delimiter"].join([a.upper() for a in attrs]),
                f"={json.dumps(value)}",
            ]
        )

    @staticmethod
    def _tab(title: str, content: str) -> str:
        return "\n".join([f'=== "{title}"', "", textwrap.indent(content, " " * 4), ""])

    def render(self, attrs: list[str], *values: Any) -> str:
        match len(values):
            case 0:
                c = self._default_config
                for a in attrs[:-1]:
                    c = getattr(c, a)
                assert isinstance(c, pydantic.BaseModel)
                config_value = env_value = c.model_dump(include={attrs[-1]}, mode="json")[attrs[-1]]
            case 1:
                config_value = env_value = values[0]
            case 2:
                config_value, env_value = values
            case _:
                raise TypeError(f"unknown number of values: {values=}")

        return "\n".join(
            [
                self._tab("Config file", code(self._config_file(attrs, config_value), language="yaml")),
                self._tab("Environment variable", code(self._env_var(attrs, env_value), language="shell")),
            ]
        )


def define_env(env: MacrosPlugin):
    config_options_renderer = ConfigOptionsRenderer()

    env.macro(config_options_renderer.render, name="config_options")

    @env.macro
    def include_file(rel_path, language="") -> str:
        path = Path(env.project_dir) / rel_path
        with open(path) as f:
            content = f.read().strip()

        if not language:
            lexer_cls = pygments.lexers.find_lexer_class_for_filename(path, code=content)
            if lexer_cls is not None and lexer_cls.aliases:
                language = lexer_cls.aliases[0]

        return Markup(code(content, language=language))


def on_pre_page_macros(env: MacrosPlugin) -> None:
    """Convert cross-reference links in tutorial notebooks to autoref tags.

    mkdocs-jupyter converts notebooks directly to HTML via nbconvert, bypassing the markdown pipeline where autorefs
    would normally convert [target][] and [display][target] syntax to <autoref> tags. This hook rewrites those
    references as <autoref> tags in a temp copy of the notebook so that autorefs' later on_env fix_refs pass
    can resolve them against local API anchors and external inventories.
    """
    src_path = env.page.file.src_path
    if not (src_path.startswith("tutorials/") and src_path.endswith((".py", ".ipynb"))):
        return

    original = Path(env.page.file.abs_src_path).read_text()

    processor = AutorefsInlineProcessor()
    idx = 0
    parts = []

    for match in processor.getCompiledRegExp().finditer(original):
        text, index, handled = processor.getText(original, match.end(0))
        if not handled:
            continue

        m = processor.RE_LINK.match(original, pos=index)
        if not m:
            continue

        identifier = m.group(1) or text

        tag = processor._make_tag(identifier, text)
        parts.append(original[idx : match.start(0)])
        parts.append(xml.etree.ElementTree.tostring(tag, encoding="unicode"))
        idx = m.end(0)

    parts.append(original[idx:])
    processed = "".join(parts)

    temp_dir: Path = env.conf["build_temp_dir"] / "mkdocs-jupyter-crossref"
    temp_dir.mkdir(exist_ok=True)

    temp_file = temp_dir / Path(src_path).name
    temp_file.write_text(processed)
    env.page.file.abs_src_path = str(temp_file)
