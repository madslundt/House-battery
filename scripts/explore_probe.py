"""SAFE probe: ensure idle first (retry hard), then CHARGE only (never discharge)."""
import asyncio
import json
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("fbp_local_tcp", "custom_components/house_battery/local_tcp.py")
fbp = importlib.util.module_from_spec(spec)
sys.modules["fbp_local_tcp"] = fbp
spec.loader.exec_module(fbp)
FbpLocalTcpClient = fbp.FbpLocalTcpClient


def summary(raw: dict) -> dict:
    s = (raw.get("SSumInfoList") or {}).copy()
    stor = raw.get("Storage_list") or [{}]
    st = stor[0] if isinstance(stor, list) else stor
    return {
        "soc": s.get("AverageBatteryAverageSOC"),
        "TotalChargePower": s.get("TotalChargePower"),
        "TotalBatteryOutputPower": s.get("TotalBatteryOutputPower"),
        "BatteryDischargingPower": st.get("BatteryDischargingPower"),
        "OffGridLoadPower": st.get("OffGridLoadPower"),
        "MeterTotalActivePower": s.get("MeterTotalActivePower"),
    }


async def readn(c, n):
    out = []
    for _ in range(n):
        raw = await c.async_snapshot()
        out.append(summary(raw.raw))
        await asyncio.sleep(1)
    return out


async def safe_mode(c, mode, power, min_soc=10, max_soc=100):
    for attempt in range(6):
        try:
            await c.async_close()
            await asyncio.sleep(0.5)
            await c.async_set_mode(mode, power, min_soc=min_soc, max_soc=max_soc)
            print(f"  -> {mode} {power}W OK (attempt {attempt+1})")
            return True
        except Exception as exc:
            print(f"  -> {mode} attempt {attempt+1} failed: {exc}")
            await asyncio.sleep(1.0)
    return False


async def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.30.90"
    c = FbpLocalTcpClient(host, 8080)

    print("== REVERT to IDLE (grid) -- PRIORITY ==")
    await safe_mode(c, "Idle", 0)
    ctr = await c.async_read_controls()
    print("  slot:", ctr.get("3003"), "custom:", ctr.get("3030"))
    for d in await readn(c, 4):
        print("   ", json.dumps(d))

    print("\n== CHARGE 400W (safe: draws grid, never exports) ==")
    await safe_mode(c, "Charge", 400)
    ctr = await c.async_read_controls()
    print("  slot:", ctr.get("3003"))
    for d in await readn(c, 6):
        print("   ", json.dumps(d))

    print("\n== REVERT to IDLE (grid) -- FINAL ==")
    await safe_mode(c, "Idle", 0)
    ctr = await c.async_read_controls()
    print("  slot:", ctr.get("3003"), "custom:", ctr.get("3030"))
    for d in await readn(c, 5):
        print("   ", json.dumps(d))


asyncio.run(main())
