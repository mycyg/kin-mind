import pytest

@pytest.fixture(autouse=True)
def local_test_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTMEM_GLOBAL_DIR", str(tmp_path / "global-memory"))
