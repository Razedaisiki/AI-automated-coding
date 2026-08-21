import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end golden path")
    config.addinivalue_line("markers", "crash: crash matrix (process/kill-9)")
