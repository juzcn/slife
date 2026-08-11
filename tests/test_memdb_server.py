"""Tests for the memdb plugin server — background reindex bounding (REVIEW M7)."""

import pytest; pytestmark = pytest.mark.unit


import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def restore_root_logger():
    """Importing the server reconfigures logging — restore it afterwards."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


def _import_memdb_server():
    """Import the memdb server fresh, stubbing the logging side-effect."""
    sys.modules.pop("slife.plugins.memdb.server", None)
    with patch(
        "slife.server_utils.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        return importlib.import_module("slife.plugins.memdb.server")


class TestBackgroundReindex:
    """The background reindex loop must not spin forever on a persistently
    failing embedder — it bounds consecutive no-progress batches."""

    @pytest.mark.asyncio
    async def test_gives_up_on_persistent_failure(self, restore_root_logger):
        """REVIEW M7 — indexed stays 0 forever → the loop aborts after the
        threshold instead of re-embedding the same turns forever."""
        srv = _import_memdb_server()
        srv._reindex_impl = AsyncMock(return_value={
            "indexed": 0, "remaining": 5, "complete": False,
        })

        with patch.object(srv, "_MAX_REINDEX_NO_PROGRESS", 3):
            await srv._background_reindex()

        assert srv._reindex_impl.await_count == 3  # gave up after the threshold

    @pytest.mark.asyncio
    async def test_progress_resets_and_completion_exits(self, restore_root_logger):
        """Progress resets the no-progress counter; complete ends the loop."""
        srv = _import_memdb_server()
        calls = [0]

        async def _impl(**kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return {"indexed": 5, "remaining": 3, "complete": False}
            return {"indexed": 3, "remaining": 0, "complete": True}

        srv._reindex_impl = _impl

        with patch.object(srv, "_MAX_REINDEX_NO_PROGRESS", 3):
            await srv._background_reindex()

        assert calls[0] == 2  # completed on the second batch
