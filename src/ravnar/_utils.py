import contextlib
from typing import Any


def fix_module(globals: dict[str, Any]) -> None:
    """Fix the __module__ attribute on public objects to hide internal structure.

    Put the following snippet at the end of public modules.

    ```python
    # isort: split

    from ._utils import fix_module

    fix_module(globals())
    del fix_module
    ```
    """
    for name, obj in globals.items():
        if not hasattr(obj, "__module__") or name.startswith("_"):
            continue

        with contextlib.suppress(Exception):
            obj.__module__ = globals["__name__"]
