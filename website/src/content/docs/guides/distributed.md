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

A pruned run (see [Storage](/agenticml-lite/reference/storage/)) cannot be resumed.

## Disabling tracking

`mode="disabled"` or `AGENTICML_MODE=disabled` turns `log`, `finish`, artifacts, and the `track_*` helpers into no-ops, so you can leave tracking code in place.
