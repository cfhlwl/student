import pytest
from django.core.cache import caches


@pytest.fixture(autouse=True)
def clear_security_rate_cache():
    """Keep isolated tests independent while preserving same-test limits."""
    caches["security_rate"].clear()
    yield
    caches["security_rate"].clear()
