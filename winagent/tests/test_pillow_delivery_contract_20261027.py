"""Image decoding is a base runtime dependency, not a build-machine accident."""
from pathlib import Path
import tomllib


def test_pillow_is_a_declared_base_runtime_dependency():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = project["project"]["dependencies"]
    assert any(item.lower().startswith("pillow") and ";" not in item for item in requirements)
