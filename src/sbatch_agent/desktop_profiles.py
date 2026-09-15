"""Product-managed desktop profiles selected from the configured cluster host.

The known-cluster preset contains only shared, non-credential loading steps.
User-owned environments from the private deployment catalog are deliberately
not copied into the desktop product.  Unknown clusters receive one inert
environment profile; their compute resources are always read from Slurm.
"""

from __future__ import annotations

from typing import Literal

from .profiles import StaticProfiles


KNOWN_CLUSTER_HOST = "10.158.132.77"
ProfileSource = Literal["known_cluster", "cluster_discovery", "custom"]


def _known_cluster_profiles() -> StaticProfiles:
    return StaticProfiles.model_validate({
        "environments": [
            {
                "id": "system-python312",
                "version": "1",
                "load_steps": [{
                    "executable": "export",
                    "args": ["PATH=/usr/bin:/bin"],
                }],
                "analysis_capabilities": {
                    "python_version": "3.12.11",
                    "dependencies": [],
                    "software": ["python", "python3"],
                },
            },
            {
                "id": "shared-conda-base",
                "version": "1",
                "load_steps": [
                    {
                        "executable": "source",
                        "args": ["/home/share/miniconda/etc/profile.d/conda.sh"],
                    },
                    {
                        "executable": "conda",
                        "args": ["activate", "/home/share/miniconda"],
                    },
                ],
                "analysis_capabilities": {
                    "python_version": "3.13.12",
                    "dependencies": [],
                    "software": ["python", "python3"],
                },
            },
            {
                "id": "shared-sci",
                "version": "1",
                "load_steps": [
                    {
                        "executable": "source",
                        "args": ["/home/share/miniconda/etc/profile.d/conda.sh"],
                    },
                    {
                        "executable": "conda",
                        "args": ["activate", "/home/share/miniconda/envs/sci"],
                    },
                ],
                "analysis_capabilities": {
                    "python_version": "3.12.13",
                    "dependencies": ["numpy", "scipy", "matplotlib"],
                    "software": ["python", "python3"],
                },
            },
            {
                "id": "shared-pygamd",
                "version": "1",
                "load_steps": [
                    {
                        "executable": "source",
                        "args": ["/home/share/miniconda/etc/profile.d/conda.sh"],
                    },
                    {
                        "executable": "conda",
                        "args": ["activate", "/home/share/miniconda/envs/pygamd"],
                    },
                ],
                "analysis_capabilities": {
                    "python_version": "3.9.25",
                    "dependencies": ["numpy", "numba", "cudatoolkit", "pygamd"],
                    "software": ["python", "python3", "pygamd"],
                },
            },
            {
                "id": "system-toolchain",
                "version": "1",
                "load_steps": [{
                    "executable": "export",
                    "args": ["PATH=/usr/bin:/bin"],
                }],
                "analysis_capabilities": {
                    "dependencies": [],
                    "software": ["gcc", "make", "cmake"],
                },
            },
            {
                "id": "gromacs-2026",
                "version": "1",
                "load_steps": [
                    {
                        "executable": "source",
                        "args": ["/etc/profile.d/spack-modules.sh"],
                    },
                    {
                        "executable": "module",
                        "args": ["load", "gromacs/2026.0-gcc-14.3.1-ooibzvm"],
                    },
                ],
                "analysis_capabilities": {
                    "dependencies": [],
                    "software": ["gromacs", "gmx", "gmx_mpi"],
                },
            },
        ],
        "launchers": [],
    })


def _discovered_cluster_profiles() -> StaticProfiles:
    # Slurm reports compute capacity, not safe environment activation commands.
    # An empty loading sequence is explicit and never guesses modules or paths.
    return StaticProfiles.model_validate({
        "environments": [{
            "id": "cluster-default",
            "version": "1",
            "load_steps": [],
        }],
        "launchers": [],
    })


KNOWN_CLUSTER_PROFILES = _known_cluster_profiles()
DISCOVERED_CLUSTER_PROFILES = _discovered_cluster_profiles()


def managed_profiles(host: str) -> tuple[StaticProfiles, ProfileSource]:
    """Return the audited shared preset or the non-guessing fallback."""
    if host == KNOWN_CLUSTER_HOST:
        return KNOWN_CLUSTER_PROFILES.model_copy(deep=True), "known_cluster"
    return DISCOVERED_CLUSTER_PROFILES.model_copy(deep=True), "cluster_discovery"


def profile_source(profiles: StaticProfiles, host: str) -> ProfileSource:
    """Classify a loaded document without trusting comments or sidecar state."""
    if host == KNOWN_CLUSTER_HOST and profiles == KNOWN_CLUSTER_PROFILES:
        return "known_cluster"
    if host != KNOWN_CLUSTER_HOST and profiles == DISCOVERED_CLUSTER_PROFILES:
        return "cluster_discovery"
    return "custom"
