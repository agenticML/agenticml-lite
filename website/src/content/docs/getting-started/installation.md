---
title: Installation
description: Install agenticml-lite and its optional extras.
sidebar:
  order: 1
---

Install the package as `agenticml-lite` and import it as `agenticml`. It needs Python 3.10 or newer.

```bash
pip install agenticml-lite
```

:::note[Which version you get]
`pip install agenticml-lite` installs 3.0.0, which adds the self-hosted server and the hosted service. To stay on the 1.0.0 local tracker, which cannot sync to a server, run `pip install agenticml-lite==1.0.0`. See [Releases](/agenticml-lite/releases/) for what each version includes.
:::

## Optional extras

| Extra | Adds | Install |
| --- | --- | --- |
| `system` | CPU, memory, and disk metrics via psutil | `pip install "agenticml-lite[system]"` |
| `gpu` | NVIDIA GPU metrics via pynvml | `pip install "agenticml-lite[gpu]"` |
| `media` | Pillow, for logging numpy arrays and PIL images | `pip install "agenticml-lite[media]"` |

System metrics are sampled every 10 seconds when `psutil` is installed.

### Server extras

These extras are for the server. They need version 3.0 (see above).

| Extra | Adds |
| --- | --- |
| `postgres` | PostgreSQL database backend |
| `s3` | Amazon S3 object storage |
| `firebase` | Firebase sign-in |
| `hosted` | `postgres`, `s3` and `firebase` together |

## Next

Continue to the [quickstart](/agenticml-lite/getting-started/quickstart/).
