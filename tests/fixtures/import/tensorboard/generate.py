"""Generate the TensorBoard import fixtures (run in a scratch venv with
tensorboardX, torch, tensorboard, numpy and Pillow installed).

    python generate.py OUT_DIR

Writes real event files with tensorboardX and torch.utils.tensorboard, then
renames them to host-independent names (the real names embed the host name).
"""
import math
import os
import shutil
import sys

import numpy as np

OUT = sys.argv[1]
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)

import tensorboardX
import torch
from torch.utils.tensorboard import SummaryWriter as TorchWriter
from tensorboardX import SummaryWriter as XWriter

rng = np.random.default_rng(7)


def image(seed, size=8):
    r = np.random.default_rng(seed)
    return r.integers(0, 255, size=(3, size, size), dtype=np.uint8)


# 1. tensorboardX: scalars over 50 steps (loss, acc), an image, a histogram, text
w = XWriter(os.path.join(OUT, "tbx_basic"))
for step in range(50):
    w.add_scalar("train/loss", math.exp(-step / 15) + 0.01 * step % 3 * 0.01, step)
    if step % 5 == 0:
        w.add_scalar("val/acc", 1 - math.exp(-step / 20), step)
w.add_image("sample", image(1), 49, dataformats="CHW")
w.add_histogram("weights", rng.normal(size=200), 10)
w.add_histogram("grads", rng.normal(size=200), 20)
w.add_text("note", "hello", 0)
w.close()

# 2. torch: scalars, an image and add_hparams (writes a child directory)
w = TorchWriter(os.path.join(OUT, "torch_hparams"))
for step in range(50):
    w.add_scalar("loss", 2.0 / (1 + step), step)
w.add_image("sample", torch.from_numpy(image(2)), 49)
w.add_hparams(
    {"lr": 0.01, "batch_size": 32, "optimizer": "adam", "bn": True},
    {"hparam/accuracy": 0.91, "hparam/loss": 0.12},
)
w.close()

# 3. torch add_scalars: loss_train and loss_val child directories (two runs)
w = TorchWriter(os.path.join(OUT, "torch_scalars"))
for step in range(20):
    w.add_scalars("loss", {"train": 1.0 / (1 + step), "val": 1.5 / (1 + step)}, step)
w.close()

# rename event files so the host name and pid are not committed
for directory, _dirs, files in os.walk(OUT):
    for index, name in enumerate(sorted(f for f in files if "tfevents" in f)):
        stamp = name.split("tfevents.")[1].split(".")[0]
        os.rename(
            os.path.join(directory, name),
            os.path.join(directory, f"events.out.tfevents.{stamp}.host.1.{index}"),
        )
print("tensorboardX", tensorboardX.__version__, "torch", torch.__version__)
