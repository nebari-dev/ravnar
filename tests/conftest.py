import os

import httpx
import pytest

from tests.utils import Sentinels, make_app_client, safe_extract_response_content


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
            raise httpx.HTTPStatusError(
                f"{error}\nResponse content: {content}"
                if (content := safe_extract_response_content(self))
                else str(error),
                request=self.request,
                response=self,
            ) from None

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


@pytest.fixture
def sentinels():
    yield Sentinels()
