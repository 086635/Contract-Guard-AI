"""Native image threading is limited only inside Windows worker processes."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from worker import main


@pytest.mark.parametrize("platform,expected", [("nt", 1), ("posix", 0)])
def test_worker_image_threads(monkeypatch: pytest.MonkeyPatch, platform: str, expected: int) -> None:
    set_threads = Mock()
    monkeypatch.setattr(main, "os", SimpleNamespace(name=platform))
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(setNumThreads=set_threads))
    main.initialize_image_runtime()
    assert set_threads.call_count == expected
    if expected:
        set_threads.assert_called_once_with(1)
