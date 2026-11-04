import pytest
from django.core.cache import caches
from django.conf import settings
from pathlib import Path


@pytest.fixture(autouse=True)
def clear_security_rate_cache():
    """Keep isolated tests independent while preserving same-test limits."""
    caches["security_rate"].clear()
    yield
    caches["security_rate"].clear()


@pytest.fixture(autouse=True)
def clear_collection_maintenance_state():
    """Keep the process-wide maintenance marker isolated between tests."""
    path = Path(settings.COLLECTION_MAINTENANCE_STATE_PATH)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)
