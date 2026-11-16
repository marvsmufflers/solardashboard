"""``solardash mqtt-test``: prove the broker link before trusting Home Assistant with it."""

from __future__ import annotations

import asyncio
import sys

from ..config import Config
from ..core.service import Service


async def _run(cfg: Config, seconds: float) -> int:
    if not cfg.mqtt.enabled:
        print("mqtt.enabled is false in config.yaml; enabling it for this test only")
        cfg.mqtt.enabled = True
    svc = Service(cfg)
    if svc.mqtt is None:
        print("no MQTT publisher built (check config)", file=sys.stderr)
        return 2
    print(f"connecting to {cfg.mqtt.host}:{cfg.mqtt.port} as {cfg.mqtt.client_id!r} (prefix {cfg.mqtt.prefix!r}, "
          f"discovery {'on' if cfg.mqtt.ha_discovery else 'off'})")
    await svc.start()
    try:
        ok = await svc.mqtt.wait_connected(10.0)
        if not ok:
            print(f"FAILED: not connected after 10 s ({svc.mqtt.last_error or 'no response from broker'})", file=sys.stderr)
            return 1
        print(f"connected; collecting device data for {seconds:.0f} s ...")
        await asyncio.sleep(seconds)
        if svc.forecast is not None:
            try:
                await asyncio.to_thread(svc.forecast.recompute)
            except Exception as e:  # noqa: BLE001
                print(f"(forecast not available: {e})")
        svc.mqtt.publish_snapshot()
        await asyncio.sleep(1.0)
        topics = svc.mqtt.published_topics()
        st = svc.mqtt.stats
        print(f"published {st['published']} messages: {len(topics)} state topics, {st['discovered']} discovery configs")
        by_dev: dict[str, int] = {}
        for t in topics:
            parts = t.split("/")
            if len(parts) >= 3:
                by_dev[parts[1]] = by_dev.get(parts[1], 0) + 1
        for dev, n in sorted(by_dev.items()):
            print(f"  {dev:<16} {n:>3} topics")
        for t in topics:
            if any(k in t for k in ("battery_state_of_charge", "pv_power/state", "recommended_now", "generator_running", "load_power/state")):
                print(f"  e.g. {t} = {svc.mqtt._last[t]}")
        print("In Home Assistant: Settings -> Devices & services -> MQTT -> devices 'Inverter', 'Totals', 'Battery n', 'Plan' ...")
        return 0
    finally:
        await svc.stop()


def run_mqtt_test(cfg: Config, seconds: float = 20.0) -> int:
    return asyncio.run(_run(cfg, seconds))
