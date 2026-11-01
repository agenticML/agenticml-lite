---
title: Configuration
description: Environment variables that configure agenticml-lite.
sidebar:
  order: 1
---

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTICML_DIR` | `~/.agenticml` | Store root. `init(dir=)` and `--dir` win. |
| `AGENTICML_MODE` | `local` | `local`, `offline` (alias of `local`), or `disabled`. |
| `AGENTICML_HOST` | `https://api.agenticml.xyz` | Server for `login`, `whoami`, and `sync`. |
| `AGENTICML_API_KEY` | unset | Key for `sync` and `whoami`. `login` ignores it. |
| `AGENTICML_CONFIG_DIR` | `~/.config/agenticml` | Credential directory. Wins over `XDG_CONFIG_HOME`. |
| `AGENTICML_MAX_BYTES` | 5 GiB | Warn when the local store grows past this. Nothing is deleted automatically. |
| `AGENTICML_MEDIA_MAX_PER_KEY` | `64` | Media rows kept per key; older rows are thinned by step. |
| `AGENTICML_MEDIA_MAX_PX` | `1024` | Longest side of a logged image, when Pillow is installed. |

`agenticml run` options have their own variables, listed under [Zero-code runs](/agenticml-lite/guides/zero-code/#options-and-environment-variables).

Boolean switches treat `0`, `false`, `no`, and `off` as off.
