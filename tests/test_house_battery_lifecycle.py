"""An unsuccessful Home Assistant unload leaves the loaded entry usable."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery import async_unload_entry
from house_battery.const import DOMAIN
from test_house_battery_cutoff_gate import coordinator


@pytest.mark.parametrize("failure", ["refused", "exception", "close_exception", "cancelled"])
def test_failed_unload_restores_fresh_updates_and_control(failure):
    async def scenario():
        coord, client, _, _ = await coordinator()
        coord.async_set_updated_data(await coord._async_update_data())
        old_status = coord.data
        entry = coord.entry
        entry.runtime_data = coord

        async def close():
            if failure == "close_exception":
                raise RuntimeError("client close failed")

        client.async_close = close

        async def unload_platforms(entry, platforms):
            if failure == "exception":
                raise RuntimeError("platform unload failed")
            if failure == "cancelled":
                raise asyncio.CancelledError
            return False

        facade = SimpleNamespace(
            data={DOMAIN: {entry.entry_id: coord}},
            config_entries=SimpleNamespace(async_unload_platforms=unload_platforms),
        )
        if failure == "refused":
            assert not await async_unload_entry(facade, entry)
        elif failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await async_unload_entry(facade, entry)
        else:
            with pytest.raises(RuntimeError, match="failed"):
                await async_unload_entry(facade, entry)

        assert not coord._unloading
        assert facade.data[DOMAIN][entry.entry_id] is coord
        client.soc = 49
        refreshed = await coord._async_update_data()
        assert refreshed is not old_status
        assert refreshed["soc"] == 49
        assert not any(call[:2] == ("mode", "Discharge") for call in client.calls[-1:])
        await coord.async_set_execution_enabled(False)
        assert not coord.runtime.execution_enabled

    asyncio.run(scenario())


def test_successful_unload_keeps_commands_revoked_and_removes_entry():
    async def scenario():
        coord, client, _, _ = await coordinator()
        entry = coord.entry
        entry.runtime_data = coord
        closed = False

        async def close():
            nonlocal closed
            assert client.calls[-1] == ("grid_idle", 20, 90)
            closed = True

        client.async_close = close

        async def unload_platforms(entry, platforms):
            return True

        facade = SimpleNamespace(
            data={DOMAIN: {entry.entry_id: coord}},
            config_entries=SimpleNamespace(async_unload_platforms=unload_platforms),
        )
        assert await async_unload_entry(facade, entry)
        assert closed
        assert coord._unloading
        assert entry.entry_id not in facade.data[DOMAIN]

    asyncio.run(scenario())
