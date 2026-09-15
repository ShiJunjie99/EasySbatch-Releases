"""Harmless standard-library smoke, executed only after explicit submission."""

import argparse
import os
from pathlib import Path
import platform
import pwd
import socket
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-directory", type=Path)
    args = parser.parse_args()
    print("hostname=" + socket.gethostname(), flush=True)
    print("cwd=" + os.getcwd(), flush=True)
    print("username=" + pwd.getpwuid(os.geteuid()).pw_name, flush=True)
    print("Python version=" + platform.python_version(), flush=True)
    print("SLURM_JOB_ID=" + os.environ.get("SLURM_JOB_ID", "unset"), flush=True)
    project_readable = Path(__file__).read_bytes().startswith(b'"""')
    print("project_readable=" + str(project_readable), flush=True)
    # Only presence is reported: never dump the environment or credential values.
    backend_present = any(name.startswith("SBATCH_AGENT_") for name in os.environ)
    print("backend_configuration_present=" + str(backend_present), flush=True)
    if args.run_directory is not None:
        readable = (args.run_directory / "submit.sh").read_bytes().startswith(b"#!/usr/bin/env bash\n")
        print("run_script_readable=" + str(readable), flush=True)
        if not readable:
            raise RuntimeError("Run script is not readable")
    if not project_readable or backend_present:
        raise RuntimeError("Deployment isolation or filesystem check failed")
    time.sleep(3)


if __name__ == "__main__":
    main()
