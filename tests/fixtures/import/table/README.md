# Table import fixtures

Fixtures for `agenticml import csv` (`src/agenticml/importers/table.py`).

| Directory | What |
| --- | --- |
| `lightning/lightning_logs/version_0`, `version_1` | PyTorch Lightning `CSVLogger` output: sparse `metrics.csv`, flat `hparams.yaml` (with one nested list that is skipped) |
| `hf/bert-finetune` | Hugging Face `Trainer` output directory: its own `trainer_state.json` plus `checkpoint-10` and `checkpoint-20` copies (only the directory's own file is imported) |
| `hf/only-checkpoints` | An output directory with only `checkpoint-10` and `checkpoint-20` (the newest is imported) |
| `plain` | `train.csv` (step + Unix-seconds timestamp + a text column) and `train.jsonl` (`global_step`, millisecond `time`, nested `train.loss`, a text field) |

## How they were made

`generate.py` writes all of them deterministically:

```
python generate.py <empty output dir>     # then copy lightning/ hf/ plain/ here
```

They were written by that script following the real file formats from the tools'
documented behaviour. They were **not** produced by running the real tools:

- PyTorch Lightning: not run (no version recorded). Format followed: `CSVLogger`
  (lightning 2.x), header `epoch,step,<metrics sorted>`, one sparse row per log call.
- Hugging Face Transformers: not run (no version recorded). Format followed:
  `trainer_state.json` of transformers 4.4x.

The files are tiny and contain no personal data. `tests/test_import_table.py` writes
its hostile-input files at test time instead of committing them.
