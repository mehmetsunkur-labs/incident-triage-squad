import pytest

from triage.config import load_settings


@pytest.fixture(scope="session")
def data_dir():
    return load_settings().data_dir
