"""Minimal CLI dispatch tests for bakeoff.quality.optimizer.main."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from bakeoff.quality.optimizer.main import main


def test_islands_dispatches_to_run_v2():
    """The 'islands' subcommand invokes PerModelOrchestrator.run_v2 with the offline backend."""
    mock_run_v2 = AsyncMock(return_value={})

    with patch(
        "bakeoff.quality.optimizer.orchestrator.PerModelOrchestrator.run_v2", mock_run_v2,
    ):
        rc = main(["islands", "--backend", "offline"])

    assert rc == 0
    mock_run_v2.assert_called_once()
    call_kwargs = mock_run_v2.call_args
    # Positional: models, backend
    assert len(call_kwargs.args) >= 2
    # Keyword: emitter, store, all_items
    assert "emitter" in call_kwargs.kwargs
    assert "store" in call_kwargs.kwargs
    assert "all_items" in call_kwargs.kwargs
    assert len(call_kwargs.kwargs["all_items"]) > 0
