"""Public feed reconnects retain verified trust, not per-socket CA copies."""

import asyncio
import ssl

import pytest

from pm_nautilus.market import PublicMarketHub
from test_market_efficiency import IdleSocket


def test_all_public_groups_share_one_verified_context(monkeypatch):
    async def run():
        hub = PublicMarketHub()
        context = ssl.create_default_context()
        contexts, factory_calls = [], []
        entered = asyncio.Event()

        def factory():
            factory_calls.append(True)
            return context

        def connect(url, **kwargs):
            assert url.startswith("wss://")
            contexts.append(kwargs.pop("ssl"))
            assert kwargs == {
                "ping_interval": 10,
                "ping_timeout": 10,
                "max_size": 16 * 1024 * 1024,
            }
            if len(contexts) == 64:
                entered.set()
            return IdleSocket()

        monkeypatch.setattr("pm_nautilus.market.ssl.create_default_context", factory)
        monkeypatch.setattr("pm_nautilus.market.connect", connect)
        try:
            for number in range(64):
                group = (str(number),)
                hub.active_groups[group] = {str(number)}
                hub.tasks[group] = asyncio.create_task(hub.socket(group))
            await asyncio.wait_for(entered.wait(), 2)
            assert len(factory_calls) == 1
            assert all(item is context for item in contexts)
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        finally:
            await hub.close()

    asyncio.run(run())


@pytest.mark.parametrize("factory_fails_first", [False, True])
def test_reconnect_reuses_context_and_failed_creation_can_retry(monkeypatch, factory_fails_first):
    async def run():
        hub = PublicMarketHub()
        group = ("1",)
        hub.active_groups[group] = {"1"}
        context = ssl.create_default_context()
        contexts, attempts = [], []
        sleep = asyncio.sleep

        def factory():
            attempts.append(True)
            if factory_fails_first and len(attempts) == 1:
                raise OSError("controlled trust-store read failure")
            return context

        class Socket(IdleSocket):
            async def __aenter__(self):
                if len(contexts) == 1:
                    raise ConnectionError("controlled public reconnect")
                return self

        def connect(*args, **kwargs):
            contexts.append(kwargs["ssl"])
            return Socket()

        async def receive(*args):
            hub.closed = True

        async def yield_without_backoff(delay):
            await sleep(0)

        monkeypatch.setattr("pm_nautilus.market.ssl.create_default_context", factory)
        monkeypatch.setattr("pm_nautilus.market.connect", connect)
        monkeypatch.setattr(hub, "receive", receive)
        monkeypatch.setattr("pm_nautilus.market.asyncio.sleep", yield_without_backoff)
        try:
            await asyncio.wait_for(hub.socket(group), 2)
            assert len(contexts) == 2 and all(item is context for item in contexts)
            assert len(attempts) == 1 + factory_fails_first
            assert not hub.sockets
        finally:
            await hub.close()

    asyncio.run(run())


def test_new_service_gets_fresh_default_trust_configuration():
    async def run():
        first, second = PublicMarketHub(), PublicMarketHub()
        try:
            a, b = first._websocket_ssl_context(), second._websocket_ssl_context()
            default = ssl.create_default_context()
            assert a is not b
            for context in (a, b):
                assert context.check_hostname == default.check_hostname is True
                assert context.verify_mode == default.verify_mode == ssl.CERT_REQUIRED
                assert context.verify_flags == default.verify_flags
                assert context.minimum_version == default.minimum_version
                assert context.options == default.options
                assert context.get_ca_certs(binary_form=True) == default.get_ca_certs(
                    binary_form=True
                )
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())
