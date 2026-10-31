# agenticml-lite

agenticml-lite is a local-first experiment tracker. Training writes metrics,
config, code, and media to files on disk, and `agenticml ui` serves a
dashboard from those files. There is no server, database, or Docker image to
run. Uploading to [AgenticML](https://agenticml.xyz) is optional.

!!! note "Pre-alpha"
    The API is pre-alpha (`0.1.0.dev0`) and may change between releases.

## Install

```bash
pip install agenticml-lite
```

Optional extras:

```bash
pip install "agenticml-lite[system]"   # psutil: CPU, memory, disk
pip install "agenticml-lite[gpu]"      # NVIDIA GPU metrics via pynvml
pip install "agenticml-lite[media]"    # Pillow, for numpy and PIL images
```

## Quickstart

```python
import agenticml

agenticml.init(
    project="demo",
    name="exp1",
    config={"learning_rate": 0.01, "epochs": 10},
)

for step in range(10):
    agenticml.log({"loss": 1 / (step + 1), "accuracy": step / 10}, step=step)

agenticml.finish()
```

Then open the dashboard:

```bash
agenticml ui
```

## Track a script without changing it

```bash
agenticml run train.py --lr 1e-3
```

## Learn more

- [README](https://github.com/agenticML/agenticml-lite#readme): full feature
  and CLI reference.
- [Issues](https://github.com/agenticML/agenticml-lite/issues): bugs and
  feature requests.
