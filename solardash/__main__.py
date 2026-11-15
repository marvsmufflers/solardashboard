"""Command line entry point.

    solardash serve   [--config config.yaml] [--simulate]
    solardash probe   list|inverter|battery|sniff|raw ...
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="solardash")
    p.add_argument("--config", "-c", default=None, help="path to config.yaml (default: ./config.yaml or $SOLARDASH_CONFIG)")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the monitoring service and web UI")
    s.add_argument("--simulate", action="store_true", help="use simulated devices")
    s.add_argument("--port", type=int, default=None, help="web port override")
    pr = sub.add_parser("probe", help="serial bus diagnostics")
    from .tools.probe import add_probe_args

    add_probe_args(pr)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.cmd in (None, "serve"):
        if getattr(args, "simulate", False):
            cfg.simulate = True
            if not cfg.batteries:
                from .config import BatteryConfig

                cfg.batteries = [BatteryConfig(name=f"Battery {i}", port=f"sim{i}") for i in (1, 2, 3)]
            cfg.chargeverter.enabled = True
            if cfg.chargeverter.mode == "off":
                cfg.chargeverter.mode = "sniff"
        if getattr(args, "port", None):
            cfg.web.port = args.port
        from .api.app import run

        run(cfg)
        return 0
    if args.cmd == "probe":
        from .tools.probe import run_probe

        return run_probe(args)
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
