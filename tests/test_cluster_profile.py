"""Public ClusterProfile validation stays non-secret and portable."""

import pytest

from sbatch_agent.cluster_profile import ClusterProfile


def test_cluster_profile_round_trips_public_metadata_only():
    profile = ClusterProfile("my-cluster", "My HPC Cluster", "cluster.example.edu", 22)
    assert ClusterProfile.from_mapping(profile.to_mapping()) == profile
    assert "password" not in repr(profile).lower()
    assert "api" not in repr(profile).lower()


@pytest.mark.parametrize("value", [
    {"id": "bad id", "display_name": "Cluster", "host": "cluster.example.edu", "ssh_port": 22},
    {"id": "cluster", "display_name": "Cluster", "host": "bad host", "ssh_port": 22},
    {"id": "cluster", "display_name": "Cluster", "host": "cluster.example.edu", "ssh_port": 0},
])
def test_cluster_profile_rejects_invalid_metadata(value):
    with pytest.raises(ValueError):
        ClusterProfile.from_mapping(value)
