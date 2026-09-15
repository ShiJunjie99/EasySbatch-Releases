"""Foreground loopback AI Relay. No SSH, credential files, DB or project access.

By default use existing provider environment. --config explicitly reuses the
existing private local Web TOML loader; it never writes configuration or tokens.
"""

import argparse
import logging
import os
from pathlib import Path
import resource

from sbatch_agent.ai_relay import create_relay_app
from sbatch_agent.model_client import ModelUnavailableError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if args.config:
            from start_web import ROOT, load_settings
            values, _ = load_settings(args.config, ROOT)
            key_env = values["SBATCH_AGENT_AI_API_KEY_ENV"]
            os.environ.update({k: v for k, v in values.items()
                               if k.startswith("SBATCH_AGENT_AI_") or k in {key_env, "SSL_CERT_FILE"}})
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        app = create_relay_app()
    except (ValueError, TypeError, OSError, ModelUnavailableError):
        print("AI Relay configuration unavailable; check local provider and independent relay token.")
        return 2
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
