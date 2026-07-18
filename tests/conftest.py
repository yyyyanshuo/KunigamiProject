import pytest
import os
import sys
import tempfile

@pytest.fixture(scope="session")
def project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@pytest.fixture(scope="session")
def app_client():
    """Create a Flask test client. Runs once per session."""
    from app import app
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    with app.test_client() as client:
        with app.app_context():
            yield client

@pytest.fixture
def tmp_json_file():
    """Create a temporary JSON file for testing safe_save_json."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)
