"""agenticml — Python SDK for the AgenticML platform.

Install as ``agenticml-py``, import as ``agenticml``.

Quickstart::

    import agenticml

    agenticml.init(project="demo", name="exp1", config={"lr": 0.01})
    for step in range(10):
        agenticml.log({"loss": 1 / (step + 1)})
    agenticml.summary["best_loss"] = 0.1
    agenticml.finish()

See https://agenticml.xyz for the full reference.
"""

from ._globals import (
    config,
    finish,
    get_active_run,
    init,
    log,
    log_artifact,
    summary,
    track_dataloader,
    track_files,
    track_loss,
    track_model,
    track_optimizer,
)
from .client import Client
from .distributed import get_rank, is_main_process
from .media import Image
from .run import Run

__version__ = "0.1.0.dev0"

__all__ = [
    "Client",
    "Image",
    "Run",
    "__version__",
    "config",
    "finish",
    "get_active_run",
    "get_rank",
    "init",
    "is_main_process",
    "log",
    "log_artifact",
    "summary",
    "track_dataloader",
    "track_files",
    "track_loss",
    "track_model",
    "track_optimizer",
]
