"""Product-managed software catalogs for the desktop application."""

from __future__ import annotations

from typing import Literal

from .desktop_profiles import KNOWN_CLUSTER_HOST, KNOWN_CLUSTER_PORT
from .profiles import StaticProfiles
from .server_catalog import ServerCatalog


CatalogSource = Literal["known_cluster", "cluster_discovery", "custom"]
AUDIT_TIME = "2026-09-08T09:08:57.674444+00:00"


def _source(detail: str) -> dict[str, str]:
    return {
        "document": "Beta EasySbatch known-cluster preset",
        "section": "Shared software audit",
        "detail": detail,
    }


def _fact(
    identifier: str,
    display_name: str,
    *,
    version: str | None,
    status: str,
    scope: str,
    detail: str = "Bounded login-host inspection; no compute workload was executed",
) -> dict[str, object]:
    return {
        "id": identifier,
        "display_name": display_name,
        "version": version,
        "source": _source(detail),
        "verification_status": status,
        "last_verified_at": AUDIT_TIME,
        "verification_scope": scope,
    }


def _known_catalog(profiles: StaticProfiles) -> ServerCatalog:
    environment_rows = [
        _fact(
            "system-python312", "System Python 3.12", version="3.12.11",
            status="VERIFIED", scope="Login-host interpreter version; compute nodes not rechecked",
        ) | {
            "type": "system_python",
            "python_executable": "/usr/bin/python3",
            "environment_profile": {"id": "system-python312", "version": "1"},
            "capabilities": {
                "python_version": "3.12.11", "dependencies": [],
                "software": ["python", "python3"],
            },
        },
        _fact(
            "shared-conda-base", "Shared Conda base", version="3.13.12",
            status="DOCUMENTED", scope="Interpreter version and selected metadata; activation/imports not tested",
        ) | {
            "type": "conda",
            "python_executable": "/home/share/miniconda/bin/python3",
            "environment_profile": {"id": "shared-conda-base", "version": "1"},
            "capabilities": {
                "python_version": "3.13.12", "dependencies": [],
                "software": ["python", "python3"],
            },
        },
        _fact(
            "shared-sci", "Shared scientific Python", version="3.12.13",
            status="DOCUMENTED", scope="Interpreter and selected package metadata; activation/imports not tested",
        ) | {
            "type": "conda",
            "python_executable": "/home/share/miniconda/envs/sci/bin/python",
            "environment_profile": {"id": "shared-sci", "version": "1"},
            "capabilities": {
                "python_version": "3.12.13",
                "dependencies": ["numpy", "scipy", "matplotlib"],
                "software": ["python", "python3"],
            },
        },
        _fact(
            "shared-pygamd", "Shared PyGAMD", version="3.9.25",
            status="DOCUMENTED", scope="Interpreter and selected package metadata; activation/imports not tested",
        ) | {
            "type": "conda",
            "python_executable": "/home/share/miniconda/envs/pygamd/bin/python",
            "environment_profile": {"id": "shared-pygamd", "version": "1"},
            "capabilities": {
                "python_version": "3.9.25",
                "dependencies": ["numpy", "numba", "cudatoolkit", "pygamd"],
                "software": ["python", "python3", "pygamd"],
            },
        },
        _fact(
            "system-toolchain", "System build tools", version=None,
            status="VERIFIED", scope="Known GCC/G++/Make/CMake version commands only; no compilation",
        ) | {
            "type": "shell_profile",
            "environment_profile": {"id": "system-toolchain", "version": "1"},
            "capabilities": {
                "dependencies": [], "software": ["gcc", "make", "cmake"],
            },
        },
        _fact(
            "gromacs-2026", "GROMACS 2026 environment", version="2026.0-spack",
            status="DOCUMENTED", scope="Module metadata and login-host version; clean batch activation not tested",
        ) | {
            "type": "module",
            "environment_profile": {"id": "gromacs-2026", "version": "1"},
            "capabilities": {
                "dependencies": [], "software": ["gromacs", "gmx", "gmx_mpi"],
            },
        },
    ]
    software_rows = [
        _fact(
            "gromacs-2026", "GROMACS", version="2026.0-spack",
            status="VERIFIED",
            scope="Login-host version command; MPI/OpenMP/CUDA build capabilities only, no simulation",
        ) | {
            "aliases": ["gromacs", "gmx", "gmx_mpi"],
            "executable": "/home/share/spack/opt/spack/linux-broadwell/gromacs-2026.0-ooibzvmh6kdqbi6byfiifoajtlpf7fu6/bin/gmx_mpi",
            "environment_profile": {"id": "gromacs-2026", "version": "1"},
            "run_type": "installed",
            "parallelism": ["threads", "mpi", "gpu"],
        },
        _fact(
            "tops-2020", "TOPS", version=None, status="DOCUMENTED",
            scope="Shared executable path metadata only; program and runtime environment not verified",
        ) | {
            "aliases": ["tops", "tops2020"],
            "executable": "/home/share/TOPS2020/TOPS2020",
            "run_type": "installed",
            "parallelism": [],
        },
        _fact(
            "scft-2026", "SCFT", version=None, status="DOCUMENTED",
            scope="Shared executable path metadata only; program and runtime environment not verified",
        ) | {
            "aliases": ["scft", "scft2026"],
            "executable": "/home/share/scft/scft2026",
            "run_type": "installed",
            "parallelism": [],
        },
    ]
    compiler_rows = [
        _fact("gcc", "GCC", version="14.3.1", status="VERIFIED", scope="Login-host version command only") | {
            "kind": "c", "executable": "/usr/bin/gcc",
            "environment_profile": {"id": "system-toolchain", "version": "1"},
        },
        _fact("gxx", "G++", version="14.3.1", status="VERIFIED", scope="Login-host version command only") | {
            "kind": "cxx", "executable": "/usr/bin/g++",
            "environment_profile": {"id": "system-toolchain", "version": "1"},
        },
        _fact("make", "GNU Make", version="4.4.1", status="VERIFIED", scope="Login-host version command only") | {
            "kind": "make", "executable": "/usr/bin/make",
            "environment_profile": {"id": "system-toolchain", "version": "1"},
        },
        _fact("cmake", "CMake", version="3.30.5", status="VERIFIED", scope="Login-host version command only") | {
            "kind": "cmake", "executable": "/usr/bin/cmake",
            "environment_profile": {"id": "system-toolchain", "version": "1"},
        },
    ]
    return ServerCatalog.model_validate({
        "metadata": {
            "schema_version": 1,
            "description": "Shared known-cluster software facts; no credentials or personal environments",
            "source_document": "Beta EasySbatch public known-cluster preset",
        },
        "environments": environment_rows,
        "software": software_rows,
        "compilers": compiler_rows,
    }).validate_profiles(profiles)


def _empty_catalog(profiles: StaticProfiles) -> ServerCatalog:
    return ServerCatalog.model_validate({
        "metadata": {
            "schema_version": 1,
            "description": "No software facts discovered; Slurm does not expose environment activation",
            "source_document": "Beta EasySbatch automatic cluster configuration",
        },
        "environments": [],
        "software": [],
        "compilers": [],
    }).validate_profiles(profiles)


def managed_catalog(
    host: str, ssh_port: int, profiles: StaticProfiles,
) -> tuple[ServerCatalog, CatalogSource]:
    if (host, ssh_port) == (KNOWN_CLUSTER_HOST, KNOWN_CLUSTER_PORT):
        return _known_catalog(profiles), "known_cluster"
    return _empty_catalog(profiles), "cluster_discovery"


def catalog_source(
    catalog: ServerCatalog, host: str, ssh_port: int, profiles: StaticProfiles,
) -> CatalogSource:
    expected, source = managed_catalog(host, ssh_port, profiles)
    return source if catalog == expected else "custom"
