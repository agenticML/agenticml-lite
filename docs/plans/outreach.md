# Outreach plan: GitHub stars and adoption

Written Nov 19, 2026, from the repo's numbers on that day. The repo-side
work is roadmap package 35 ([plan](packages/35-repo-outreach.md)); the
launch and community steps are owner steps.

## Where things stand (Nov 19, 2026)

- 0 stars, 0 forks.
- GitHub traffic: 8 unique visitors in the last 14 days, mostly the team.
- PyPI: about 300 downloads a month (141 in the last week), but almost none
  of those people reach the GitHub repo.
- The repo page: no topics, the default social preview image, Discussions
  off, community health 50% (no CONTRIBUTING, code of conduct, or issue
  templates).
- The README: no images, a dense opening paragraph, 419 lines of reference
  material that the docs site already covers.

Almost nobody finds the project, and the few who land on it don't see a
quick reason to care. Fix the landing page first, then send people to it.

## 1. Make the repo convert visitors (do first)

- **Rework the top of the README.** A one-line pitch first ("Experiment
  tracking with a W&B-style API that runs from files, with no server."),
  then a screenshot or GIF of `agenticml ui`, badges (PyPI version, license,
  CI, Python versions) and a 3-line quickstart.
- **Add a short comparison table** against W&B, MLflow, TensorBoard, Aim and
  Trackio (Hugging Face's local-first tracker, the closest competitor).
- **Move the reference sections to the docs site.** Command forms, on-prem
  setup, backups and upgrades. The README is a sales page that links to the
  docs.
- **GitHub settings.** Topics (`experiment-tracking`, `mlops`,
  `machine-learning`, `pytorch`, `wandb-alternative`, `tensorboard`,
  `self-hosted`, `local-first`), a custom social preview image, Discussions
  on, CONTRIBUTING, a code of conduct, issue templates and a few
  `good first issue` labels.
- **"agentic" may mislead searchers.** Keep the name, but drop it from the
  PyPI keywords and lead with "experiment tracking".

## 2. Bring PyPI users to the repo

- A small "GitHub" link in the dashboard.
- One line from `agenticml ui` at startup. No nagging beyond that.

## 3. Launch once (after step 1)

- **Show HN** on a weekday morning (US time), with an honest technical
  title, e.g. "Show HN: Local-first experiment tracker, no server, optional
  self-host". Stay around to answer comments all day.
- **Reddit:** r/MachineLearning ([P] tag, weekday), r/LocalLLaMA,
  r/selfhosted (the self-hosted server fits it well).
- **Awesome lists:** awesome-mlops, awesome-production-machine-learning,
  awesome-selfhosted. One PR each.
- Post on X, LinkedIn and Bluesky with the GIF.

## 4. Keep it going

- **Answer people where they already ask** for a W&B alternative or offline
  experiment tracking (Reddit, Stack Overflow, GitHub issues).
- **Write 2–3 technical posts:** why file-first tracking, how the sync
  protocol works, running a tracker on an air-gapped or VPC-only cluster.
  Publish on dev.to or Medium and link them from the docs.
- **Integration examples** for PyTorch Lightning, HF Transformers `Trainer`
  callbacks, Keras and `torchrun`. People search for "<framework>
  experiment tracking".
- **A `wandb` compatibility shim** (`import agenticml as wandb` just works)
  would be a feature, not marketing, but is probably the biggest single
  adoption lever if the API is close enough.

## Who does what

- Package 35 (supervisor plans, Sonnet builds): README, community files,
  images, PyPI keywords, dashboard link and `agenticml ui` line.
- Owner (public changes to the repo and accounts): topics, social preview
  upload, Discussions, labels; launch posts and community answers. The
  supervisor can draft the posts but does not post them.
- The README on GitHub's front page changes only once the PR reaches `main`.
