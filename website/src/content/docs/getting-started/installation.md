---
title: Installation
description: Install agenticml-lite and its optional extras.
sidebar:
  order: 1
---

Install the package as `agenticml-lite` and import it as `agenticml`. It needs Python 3.9 or newer.

```bash
pip install agenticml-lite
```

:::caution[Pre-alpha]
The API is pre-alpha (`0.1.0.dev0`) and may change between releases.
:::

## Optional extras

| Extra | Adds | Install |
| --- | --- | --- |
| `system` | CPU, memory, and disk metrics via psutil | `pip install "agenticml-lite[system]"` |
| `gpu` | NVIDIA GPU metrics via pynvml | `pip install "agenticml-lite[gpu]"` |
| `media` | Pillow, for logging numpy arrays and PIL images | `pip install "agenticml-lite[media]"` |

System metrics are sampled every 10 seconds when `psutil` is installed.

## Next

Continue to the [quickstart](/agenticml-lite/getting-started/quickstart/).
