"""Distributed-training helpers.

In a multi-rank training job (DDP, FSDP, Horovod, ...), having every rank
log the same metrics is wasteful at best and corrupting at worst. The convention
is to log only from rank 0.

`is_main_process()` checks the standard rank env vars used by torchrun, mpirun,
deepspeed, accelerate, and SLURM.
"""

import os


_RANK_ENV_VARS = (
    "RANK",          # torchrun, accelerate
    "PMI_RANK",      # MPI
    "OMPI_COMM_WORLD_RANK",  # OpenMPI
    "SLURM_PROCID",  # SLURM
    "LOCAL_RANK",    # last-ditch fallback
)


def get_rank() -> int:
    for var in _RANK_ENV_VARS:
        value = os.environ.get(var)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                continue
    return 0


def is_main_process() -> bool:
    return get_rank() == 0
