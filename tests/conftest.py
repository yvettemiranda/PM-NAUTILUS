import pytest

import pm_nautilus.app as app_module
from pm_nautilus.test_accounts import TestAccounts
from pm_nautilus.test_runtime import TestRuntime


@pytest.fixture
def legacy_test_accounts(monkeypatch):
    """Keep legacy coordination regression coverage without enabling it by configuration."""
    monkeypatch.setattr(app_module, "PrimaryTestAccount", TestAccounts)


@pytest.fixture
def paused_full_feed(monkeypatch):
    """Earlier PAUSED behaviour: keep every eligible market subscribed.

    Production PAUSED TEST accounts monitor only markets with owned work. Tests
    of other shared-feed mechanisms, and of the earlier PAUSED preview mode,
    use paused accounts as ordinary full-feed consumers.
    """
    monkeypatch.setattr(TestRuntime, "paused_full_feed", True)
