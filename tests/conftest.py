import contextlib
import json
import os

import httpx
import pytest

from tests.utils import make_app_client


@pytest.fixture(autouse=True)
def ravnar_local_storage(mocker, tmp_path):
    p = tmp_path / "ravnar_local"
    p.mkdir()
    mocker.patch.dict(os.environ, {"RAVNAR_LOCAL_STORAGE": str(p)})


@pytest.fixture(scope="session", autouse=True)
def enhance_raise_for_status(session_mocker):
    raise_for_status = httpx.Response.raise_for_status

    def enhanced_raise_for_status(self: httpx.Response):
        __tracebackhide__ = True

        try:
            return raise_for_status(self)
        except httpx.HTTPStatusError as error:
            content = self.read()

            if content:
                text = f"<{len(content)} non-decodable bytes>"
                with contextlib.suppress(Exception):
                    text = content.decode()
                    text = f"\n{json.dumps(json.loads(content), indent=2)}"

                message = f"{error}\nResponse content: {text}"
            else:
                message = str(error)

            raise httpx.HTTPStatusError(message, request=self.request, response=self) from None

    yield session_mocker.patch(
        ".".join(
            [
                httpx.Response.__module__,
                httpx.Response.__name__,
                raise_for_status.__name__,
            ]
        ),
        new=enhanced_raise_for_status,
    )


@pytest.fixture
def app_client():
    with make_app_client() as client:
        yield client
