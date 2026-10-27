#!/usr/bin/env python3
"""Dependency-free toy training loop for trying out agenticml.

Zero-code tracking (this file never imports agenticml):
    agenticml run -- python examples/train_toy.py --epochs 3
Explicit logging from inside the script:
    python examples/train_toy.py --explicit --epochs 3
"""

import argparse
import random


def train(args, log=None):
    """Fit y = w*x + b by SGD on noisy data; call log(epoch, metrics) per epoch."""
    rng = random.Random(args.seed)
    data = [(x, 3.0 * x - 1.0 + rng.gauss(0, 0.1)) for x in (rng.uniform(-1, 1) for _ in range(256))]
    train_set, val_set = data[:200], data[200:]
    w = b = 0.0
    loss = acc = 0.0
    for epoch in range(args.epochs):
        loss_sum = hits = n = 0
        for _ in range(args.steps_per_epoch):
            x, y = rng.choice(train_set)
            err = (w * x + b) - y
            w -= args.lr * 2 * err * x
            b -= args.lr * 2 * err
            loss_sum += err * err
            hits += abs(err) < 0.25
            n += 1
        loss, acc = loss_sum / n, hits / n
        val_loss = sum((w * x + b - y) ** 2 for x, y in val_set) / len(val_set)
        print(f"epoch {epoch + 1}/{args.epochs} loss={loss:.4f} acc={acc:.3f} val_loss={val_loss:.4f}", flush=True)
        if log:
            log(epoch, {"train/loss": loss, "train/acc": acc, "val/loss": val_loss, "lr": args.lr}, w, b)
    return w, b, loss, acc


def make_image(w, b, size=32):
    """A small PNG-able image of the fitted line, or None without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    img = Image.new("RGB", (size, size), "white")
    for px in range(size):
        y = w * (px / (size - 1) * 2 - 1) + b
        py = int((1 - (max(-4.0, min(4.0, y)) + 4) / 8) * (size - 1))
        img.putpixel((px, py), (200, 30, 30))
    return img


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lr", type=float, default=0.1)
    p.add_argument("--epochs", type=int, default=10)
    p.add_argument("--steps-per-epoch", type=int, default=100)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--explicit", action="store_true", help="import agenticml and log by hand")
    args = p.parse_args()

    if not args.explicit:
        w, b, loss, acc = train(args)
        print(f"done: w={w:.3f} b={b:.3f} final loss={loss:.4f}")
        return

    import agenticml

    agenticml.init(project="toy", name=f"lr{args.lr}-seed{args.seed}", config=vars(args))

    def log(epoch, metrics, w, b):
        payload = dict(metrics)
        img = make_image(w, b)
        if img is not None:
            payload["fit"] = agenticml.Image(img, caption=f"w={w:.2f} b={b:.2f}")
        agenticml.log(payload, step=epoch)

    w, b, loss, acc = train(args, log)
    agenticml.summary["final_loss"] = loss
    agenticml.summary["final_acc"] = acc
    agenticml.summary["w_error"] = abs(w - 3.0) + abs(b + 1.0)
    agenticml.finish()


if __name__ == "__main__":
    main()
