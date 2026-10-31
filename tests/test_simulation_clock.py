"""Controller clocks must not modify asyncio's real deadline clock."""
import asyncio

import pytest

from .dispatch_simulation import DispatchSimulation, Simulation


@pytest.mark.parametrize('simulation', [Simulation, DispatchSimulation])
@pytest.mark.parametrize('offset', [-10000, 10000])
def test_simulated_controller_clock_is_isolated_from_asyncio(simulation, offset):
    async def scenario():
        loop = asyncio.get_running_loop()
        s = simulation()
        before = loop.time()
        s.now = before + offset
        s.sample()
        observed = []
        if simulation is DispatchSimulation:
            target = s.client._client.write_registers
            original = target.side_effect
        else:
            target = s.c.async_write_dispatch
            original = None
        async def write(*args, **kwargs):
            observed.append(loop.time())
            if original is not None:
                return await original(*args, **kwargs)
        target.side_effect = write
        await s.start()
        await s.step()
        assert s.sw.is_on and observed
        assert all(0 <= stamp - before < 2 for stamp in observed)
        assert 0 <= loop.time() - before < 2
        await s.sw.async_force_off()
    asyncio.run(asyncio.wait_for(scenario(), 5))
