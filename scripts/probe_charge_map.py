"""Live, export-safe charge probe to map FBP1200 fields.

Sequence (all over the local TCP JSON protocol, never touching relays/meter wiring):
  1. baseline EnergyParameter + control registers
  2. command Charge 200 W (grid->battery; import only, cannot export)
  3. poll telemetry ~6x over ~180 s with a live EXPORT guard
  4. command Idle (return to baseline grid-tied operation)
  5. read back controls to confirm the write landed

Export guard: MeterTotalActivePower uses import-positive convention.
If it ever reads strongly negative (grid export) we abort and return to Idle.
"""
import json
import socket
import time

HOST, PORT = "192.168.30.90", 8080
CHARGE_W = 200
CHARGE_S = 180            # seconds of charging
STEP = 30
EXPORT_ABORT_W = -80      # negative meter => export; abort if below this
IDLE_SLOT = "0,00:00,00:00,0,0,0,0,0,0,90,10"
CHARGE_SLOT = f"1,00:00,23:59,-{CHARGE_W},0,6,5,0,0,90,10"


def frame(req):
    last = None
    for attempt in range(5):
        s = socket.create_connection((HOST, PORT), timeout=8)
        try:
            s.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode())
            time.sleep(0.4)
            data = s.recv(262144).decode()
            return json.loads(data)
        except (TimeoutError, OSError) as e:
            last = e
        finally:
            s.close()
        time.sleep(1.0)  # device sometimes displaces the session; retry fresh
    raise RuntimeError(f"device did not answer ({last!r})")


def telemetry():
    d = frame({"Get": "EnergyParameter", "SerialNumber": 99, "CommandSource": "HA"})
    summ = d.get("SSumInfoList", {})
    unit = (d.get("Storage_list") or [{}])[0] if isinstance(d.get("Storage_list"), list) else {}
    return {
        "soc": summ.get("AverageBatteryAverageSOC"),
        "offgrid_load": unit.get("OffGridLoadPower"),
        "smart_load": summ.get("TotalSmartLoadElectricalPower"),
        "meter": summ.get("MeterTotalActivePower"),
        "backup": summ.get("TotalBackUpPower"),
        "pv": summ.get("TotalPVPower"),
        "dischg_u": unit.get("BatteryDischargingPower"),
        "chg_u": unit.get("BatteryChargingPower"),
        "tot_chg": summ.get("TotalChargePower"),
        "tot_dischg": summ.get("TotalBatteryOutputPower"),
    }


def controls():
    d = frame({
        "Get": "Energycontrolparameters",
        "RegControlAddr": [3000, 3003, 3020, 3021, 3022, 3023, 3024, 3030],
        "SerialNumber": 99, "CommandSource": "HA",
    })
    return d.get("ControlInfo", {})


def set_slot(slot):
    frame({
        "Set": "Energycontrolparameters",
        "SetControlInfo": {
            "3000": "1", "3020": "6", "3021": "0", "3022": "0",
            "3030": "1", "3003": slot,
        },
        "SerialNumber": 99, "CommandSource": "HA",
    })
    time.sleep(1.5)


def row(t):
    exp = t["meter"] if isinstance(t["meter"], (int, float)) else None
    export = exp is not None and exp < EXPORT_ABORT_W
    flag = " *** EXPORT DETECTED - ABORT ***" if export else ""
    return (
        f"soc={t['soc']:>4}  offgrid={t['offgrid_load']:>5}  "
        f"smart_load={t['smart_load']:>5}  meter={str(t['meter']):>6}  "
        f"backup={t['backup']:>5}  pv={t['pv']:>4}  "
        f"dischg_U={t['dischg_u']:>5}  chg_U={t['chg_u']:>5}  "
        f"tot_chg={t['tot_chg']:>5}  tot_dischg={t['tot_dischg']:>6}{flag}"
    )


def main():
    print("== BASELINE ==")
    print("controls:", controls())
    t = telemetry(); print("telemetry:", row(t))

    print("\n== COMMAND CHARGE {} W ==".format(CHARGE_W))
    set_slot(CHARGE_SLOT)
    print("controls after write:", controls())

    aborted = False
    t0 = time.time()
    try:
        while time.time() - t0 < CHARGE_S:
            t = telemetry()
            print(row(t))
            if t["meter"] is not None and t["meter"] < EXPORT_ABORT_W:
                print("\n[SAFETY] grid export detected during charge -> aborting")
                aborted = True
                break
            time.sleep(STEP)
    finally:
        print("\n== COMMAND IDLE (return to grid/baseline) ==")
        set_slot(IDLE_SLOT)
        print("controls after idle:", controls())
        tb = telemetry()
        print("telemetry after idle:", row(tb))

    print("\nDONE. aborted_export =", aborted)


if __name__ == "__main__":
    main()
