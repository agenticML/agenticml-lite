# Examples

`train_toy.py` is a pure-Python toy training loop (no dependencies; Pillow
is optional and only used to log a small image).

Zero-code tracking: the script never imports agenticml, and `agenticml run`
captures its argparse hyperparameters, code and system metrics for you.

    agenticml run -- python examples/train_toy.py --lr 0.05 --epochs 5

Explicit logging: the script calls `init`, `log`, `summary` and `finish` itself.

    python examples/train_toy.py --explicit --lr 0.05 --epochs 5

Runs land in `~/.agenticml` (override with `--dir` or `AGENTICML_DIR`).

    agenticml ls        # list runs
    agenticml ui        # browse them at http://127.0.0.1:6006
