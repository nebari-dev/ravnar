import urllib.error
import urllib.request
from typing import Annotated

import rich
import typer
from fastapi import status

import ravnar
from _ravnar.config import Config
from _ravnar.core import Ravnar

app = typer.Typer(
    name="ravnar",
    invoke_without_command=True,
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def version_callback(value: bool) -> None:
    if value:
        rich.print(f"ravnar {ravnar.__version__} from {ravnar.__path__[0]}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool | None, typer.Option("--version", callback=version_callback, help="Show version and exit.")
    ] = None,
) -> None:
    pass


@app.command(help="Serve REST API.")
def serve() -> None:
    Ravnar().serve()


@app.command(help="Check health of the server")
def health() -> None:
    config = Config.parse()

    def is_healthy() -> bool:
        try:
            return (  # type: ignore[no-any-return]
                urllib.request.urlopen(f"http://{config.server.hostname}:{config.server.port}/health").status
                == status.HTTP_200_OK
            )
        except urllib.error.URLError:
            return False

    raise typer.Exit(code=int(not is_healthy()))


@app.command(help="Output the configuration as JSON")
def config(*, pretty: Annotated[bool, typer.Option(help="Pretty-print the output")] = False) -> None:
    config = Config.model_validate({})
    print(config.model_dump_json(indent=2 if pretty else None))
