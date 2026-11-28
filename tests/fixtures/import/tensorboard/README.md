# TensorBoard import fixtures

`logdir/` holds real event files (about 28 KB in total), written once by the
real writers and committed. `generate.py` is the generator. Only
`tests/test_import_tensorboard.py` reads them.

| Directory | Written by | Content |
| --- | --- | --- |
| `tbx_basic/` | `tensorboardX.SummaryWriter` | `train/loss` for 50 steps, `val/acc` every 5th step, one 8x8 image `sample` at step 49, two histograms, one text |
| `torch_hparams/` | `torch.utils.tensorboard.SummaryWriter` | `loss` for 50 steps, one image, and `add_hparams` (a child directory named by a timestamp, holding the hparams and two metrics) |
| `torch_scalars/` | `torch.utils.tensorboard.SummaryWriter` | `add_scalars("loss", {"train", "val"})` for 20 steps: child directories `loss_train` and `loss_val`; the directory itself has only the `file_version` event |

The generated files are renamed to `events.out.tfevents.<unix time>.host.1.<n>`
because the real names embed the machine's host name. The importer reads any
file whose name contains `tfevents`.

Versions used to generate them (Python 3.12.13):

- tensorboardX 2.6.5
- torch 2.15.0 (`torch.utils.tensorboard`)
- tensorboard 2.21.0
- numpy 2.5.5, Pillow 13.0.0, protobuf 7.36.2

## Regenerate

```
uv venv venv-tb
uv pip install --python venv-tb/bin/python tensorboardX torch tensorboard numpy Pillow
venv-tb/bin/python generate.py /tmp/out      # then copy /tmp/out to logdir/
```

The event files carry the wall time of the run that wrote them, so a
regenerated set has different times. The test expectations on times (first
and last event, run id prefix) would then need the new values.

Quirk worth knowing: `torch` `add_hparams` stores a Python `bool` as a number
(`True` becomes 1), because it tests `isinstance(v, int)` first. The importer
shows what the file holds.

## Cross-check

`crosscheck.py` compares the importer's scalars and image sizes with what
TensorBoard's own `EventFileLoader` reads from the same files. Run it from the
repository root in the venv that has `tensorboard` installed:
`venv-tb/bin/python tests/fixtures/import/tensorboard/crosscheck.py`.
Every series matched when the fixtures were made.

## What is not here

No TensorFlow 2 writer was available, so the TF2 forms (`tensor` scalars in
`float_val` / `tensor_content`, the `images` plugin tensor) are built by hand
in the tests, using the field numbers from `tensor.proto`.
