import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from make_fixtures import KNOWN, make_all  # noqa: E402


class MockNer:
    """Dictionary-based stand-in for GLiNER: finds KNOWN names (case-sensitive)."""
    name = "mock"

    def __init__(self, known=None):
        self.known = known or KNOWN
        self.calls = 0

    def __call__(self, texts, labels, threshold):
        self.calls += 1
        out = []
        for t in texts:
            ents = []
            for n, lab in self.known.items():
                if lab not in labels:
                    continue
                for m in re.finditer(r"(?<!\w)" + re.escape(n) + r"(?!\w)", t):
                    ents.append({"start": m.start(), "end": m.end(), "text": n, "label": lab, "score": 0.9})
            out.append(ents)
        return out


@pytest.fixture
def ner():
    return MockNer()


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory):
    return make_all(tmp_path_factory.mktemp("fx"))


@pytest.fixture(autouse=True)
def scrub_home(tmp_path, monkeypatch):
    import scrub.job as job
    monkeypatch.setattr(job, "HOME", tmp_path / "home")
    monkeypatch.setattr(job, "JOBS", tmp_path / "home" / "jobs")
    return tmp_path / "home"
