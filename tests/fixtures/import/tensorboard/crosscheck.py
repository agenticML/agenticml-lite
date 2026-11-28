"""Compare the importer with TensorBoard's own EventFileLoader on the fixtures.

Run from the repository root in the venv that has tensorboard installed:

    venv-tb/bin/python tests/fixtures/import/tensorboard/crosscheck.py
"""
import glob
import sys

sys.path.insert(0, "src")

from tensorboard.backend.event_processing.event_file_loader import EventFileLoader

from agenticml.importers import ImportOptions
from agenticml.importers.tensorboard import discover

root = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/import/tensorboard/logdir"
scalars, images = {}, {}
for path in sorted(glob.glob(root + "/**/*tfevents*", recursive=True)):
    directory = path.rsplit("/", 1)[0][len(root) + 1 :] or "."
    for event in EventFileLoader(path).Load():
        for value in event.summary.value:
            plugin = value.metadata.plugin_data.plugin_name
            if plugin == "scalars":
                scalars.setdefault(directory, []).append(
                    (event.wall_time, event.step, value.tag, value.tensor.float_val[0])
                )
            elif plugin == "images":
                images.setdefault(directory, []).append(
                    (event.step, value.tag, len(value.tensor.string_val[2]))
                )

ok = True
for run in discover(root, ImportOptions()):
    key = "." if run.name == "logdir" else run.name
    mine = [(w, s, k, v) for w, s, m in run.points for k, v in m.items()]
    imgs = [(m[1], m[2], len(m[3])) for m in run.media]
    same = mine == scalars.get(key, []) and imgs == images.get(key, [])
    ok = ok and same
    print(f"{run.name:28} {len(mine):4} scalars {len(imgs)} images  {'match' if same else 'DIFFERENT'}")
sys.exit(0 if ok else 1)
