"""Minimal frozen application entrypoint shared by every client platform."""

from sbatch_agent.launcher import main


if __name__ == "__main__":
    raise SystemExit(main())
