import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def parse_fixture():
    def load(name: str) -> dict:
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["parse"]

    return load
