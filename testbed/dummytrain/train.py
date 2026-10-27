"""Run with python -m dummytrain.train; tracking is opt-in with --explicit."""
import argparse
from pathlib import Path
import random
import sys

from dummytrain.data import make_data
from dummytrain.model import LogisticRegression


def train(cfg, on_step=None, on_epoch=None):
    cfg = vars(cfg) if isinstance(cfg, argparse.Namespace) else cfg
    lr = cfg.get("lr", 0.15)
    epochs = cfg.get("epochs", 4)
    steps = cfg.get("steps_per_epoch", 8)
    batch_size = cfg.get("batch_size", 16)
    if lr <= 0 or min(epochs, steps, batch_size) < 1:
        raise ValueError("lr, epochs, steps-per-epoch and batch-size must be positive")
    seed = cfg.get("seed", 7)
    training, validation = make_data(seed)
    model = LogisticRegression(seed)
    rng = random.Random(seed)
    initial_loss = model.loss(training)
    losses, history = [], []
    for epoch in range(1, epochs + 1):
        if cfg.get("fail_at_epoch") == epoch:
            raise RuntimeError(f"requested failure at epoch {epoch}")
        for local_step in range(steps):
            batch = [rng.choice(training) for _ in range(batch_size)]
            model.step(batch, lr)
            loss = model.loss(training)
            losses.append(loss)
            if on_step:
                on_step((epoch - 1) * steps + local_step, {"train/loss": loss, "train/lr": lr})
        metrics = {"val/loss": model.loss(validation),
                   "val/acc": sum((model.forward(x) >= 0.5) == bool(y)
                                  for x, y in validation) / len(validation)}
        history.append(metrics)
        if on_epoch:
            on_epoch(epoch, metrics)
    checkpoint = Path(cfg.get("out", "checkpoints")) / "model.json"
    model.save(checkpoint)
    return {"model": model, "checkpoint": checkpoint, "initial_loss": initial_loss,
            "final_loss": losses[-1], "best_val_acc": max(m["val/acc"] for m in history),
            "losses": losses, "history": history}


def fit_image(model):
    try:
        from PIL import Image
    except ImportError:
        return None
    image = Image.new("RGB", (48, 48))
    for y in range(48):
        for x in range(48):
            p = model.forward(((x - 24) / 8, (y - 24) / 8))
            image.putpixel((x, y), (int(255 * p), 70, int(255 * (1 - p))))
    return image


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lr", type=float, default=0.15)
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--steps-per-epoch", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--project", default="testbed")
    parser.add_argument("--name", default="explicit-baseline")
    parser.add_argument("--tags", default="demo,baseline")
    parser.add_argument("--out", default="checkpoints")
    parser.add_argument("--explicit", action="store_true")
    parser.add_argument("--fail-at-epoch", type=int)
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args(argv)
    tracker = None
    if args.explicit:
        import agenticml
        tracker = agenticml
        tracker.init(args.project, args.name, config=vars(args),
                     tags=[t.strip() for t in args.tags.split(",") if t.strip()],
                     notes="Pure-Python logistic regression", group="synthetic",
                     job_type="train", verbose=True)

    def on_step(step, metrics):
        tracker.log({"train/lr": metrics["train/lr"]}, step=step, commit=False)
        tracker.log({"train/loss": metrics["train/loss"]}, step=step)

    def on_epoch(epoch, metrics):
        tracker.log(metrics, step=epoch * args.steps_per_epoch - 1)

    try:
        result = train(args, on_step if tracker else None, on_epoch if tracker else None)
        if tracker:
            image = fit_image(result["model"])
            if image is not None:
                tracker.log({"fit": tracker.Image(image, caption="decision boundary")})
            tracker.log_artifact(result["checkpoint"], name="checkpoint", type="model")
            tracker.log_artifact(result["checkpoint"], name="checkpoint", type="model", copy=True)
            tracker.summary.update({k: result[k] for k in ("initial_loss", "final_loss", "best_val_acc")})
            tracker.summary["epochs"] = args.epochs
    except BaseException as exc:
        if tracker:
            tracker.summary["_exception"] = type(exc).__name__
            tracker.finish(status="crashed")
        raise
    if tracker:
        if args.exit_code:
            tracker.summary["_exit_code"] = args.exit_code
        tracker.finish(status="crashed" if args.exit_code else "finished")
    print(f"loss {result['initial_loss']:.4f} -> {result['final_loss']:.4f}")
    if args.exit_code:
        sys.exit(args.exit_code)
    return result


if __name__ == "__main__":
    main()
