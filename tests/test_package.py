"""Foundation checks that the Collector package is actually installed."""

from importlib.metadata import metadata
from importlib.resources import files

import agent_meter


def test_package_is_importable_with_distribution_metadata() -> None:
    assert agent_meter.__version__ == "0.1.0"

    dist = metadata("agent-meter")
    assert dist["Name"] == "agent-meter"
    assert dist["Version"] == "0.1.0"
    assert files("agent_meter").joinpath("py.typed").is_file()
