import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest.mock

import fastapi.openapi.utils
import mkdocs_gen_files
import typer.rich_utils

from _ravnar.config import BaseConfig
from _ravnar.core import Ravnar
from ravnar._cli import app as cli_app


def main():
    openapi_specification()
    cli_reference()
    helm_chart_reference()


def openapi_specification() -> None:
    app = Ravnar(BaseConfig()).app
    openapi_json = fastapi.openapi.utils.get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    with mkdocs_gen_files.open("references/openapi.json", "w") as file:
        json.dump(openapi_json, file)


def cli_reference() -> None:
    prog_name = "ravnar"

    def get_help(command):
        with unittest.mock.patch.object(typer.rich_utils, "MAX_WIDTH", 80):
            with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()) as stdout:
                cli_app(([command] if command else []) + ["--help"], prog_name=prog_name)

            return "\n".join(line.strip() for line in stdout.getvalue().strip().splitlines())

    def get_doc(command):
        return "\n".join(
            [
                f"## {prog_name}{f' {command}' if command else ''}",
                "",
                "```",
                get_help(command),
                "```",
                "",
            ]
        )

    with mkdocs_gen_files.open("references/cli.md", "w") as file:
        file.write(f"# CLI reference\n\n{get_doc(None)}")
        for command in cli_app.registered_commands:
            file.write(get_doc(command.name or command.callback.__name__))


def helm_chart_reference():
    def content() -> str:
        if shutil.which("helm-docs", mode=os.X_OK) is None:
            return "[`helm-docs`](https://github.com/norwoodj/helm-docs) is required to generate the helm chart documentation."

        result = subprocess.run(
            [
                "helm-docs",
                "--chart-to-generate=helm/ravnar-chart",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    with mkdocs_gen_files.open("references/helm-chart.md", "w") as file:
        file.write(content())


main()
