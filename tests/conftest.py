import pytest

from pm_nautilus.test_runtime import TestRuntime


@pytest.fixture
def paused_full_feed(monkeypatch):
    """Earlier PAUSED behaviour: keep every eligible market subscribed.

    Production PAUSED TEST accounts monitor only markets with owned work. Tests
    of other shared-feed mechanisms, and of the earlier PAUSED preview mode,
    use paused accounts as ordinary full-feed consumers.
    """
    monkeypatch.setattr(TestRuntime, "paused_full_feed", True)
