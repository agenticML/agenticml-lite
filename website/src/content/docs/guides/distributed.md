---
title: Distributed, resume, disabled
description: Multi-GPU training, resuming runs, and turning tracking off.
sidebar:
  order: 4
---

## Distributed training

Only rank 0 logs. With `init(only_main_process=True)` (the default), every other rank gets a no-op run. The rank comes from `RANK`, `PMI_RANK`, `OMPI_COMM_WORLD_RANK`, `SLURM_PROCID`, then `LOCAL_RANK`.

Put the tracker inside the launcher so every rank runs your script:

```bash
torchrun --nproc_per_node 4 -m agenticml run -- train.py
```

To track every rank instead of rank 0 only, pass `--all-ranks` or set `AGENTICML_ALL_RANKS=1` (`only_main_process=False` in Python).

## Resuming a run

Pass the same id to resume:

```python
agenticml.init(project="demo", name="exp1", id=run_id, resume="allow")
```

| `resume=` | Behavior |
| --- | --- |
| `"never"` (default) | Rejects an `id`. |
| `"allow"` | Opens the run, or creates it if missing. |
| `"must"` | Errors if the run is missing. |

A pruned run (see [Storage](/reference/storage/)) cannot be resumed (its overview is present, but the raw series file is gone).

Under `agenticml run`, use `--id` and `--resume` instead. They are described in [Resuming a run](/guides/zero-code/#resuming-a-run).

```bash
agenticml run --id 20261101T130355-1cded4a7 --resume must -- python train.py
```

## Disabling tracking

`mode="disabled"` or `AGENTICML_MODE=disabled` turns `log`, `finish`, artifacts, and the `track_*` helpers into no-ops, so you can leave tracking code in place. With `verbose=True`, `init` prints `agenticml: tracking disabled`.
