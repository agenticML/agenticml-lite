"""Generate the MLflow fixtures: python generate.py <output dir>.

Writes <output dir>/filestore/mlruns (file store) and <output dir>/sqlite/mlflow.db.
Run it with a scratch venv that has `mlflow` installed; never in the project venv.
"""

import os
import sys
import time
from pathlib import Path

os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"  # MLflow 3.18 refuses the file store otherwise
import mlflow  # noqa: E402
from mlflow.tracking import MlflowClient  # noqa: E402


def populate(uri: str) -> None:
    mlflow.set_tracking_uri(uri)
    client = MlflowClient(uri)
    mlflow.set_experiment("resnet")

    # Run 1: finished, metrics with step, a nested key, params, tags, a note, a child run.
    with mlflow.start_run(run_name="baseline") as parent:
        mlflow.log_params({"lr": 0.1, "optimizer": "sgd", "layers": 18})
        mlflow.set_tags({"team": "vision", "mlflow.note.content": "first baseline run"})
        for step in range(5):
            mlflow.log_metric("train/loss", 1.0 / (step + 1), step=step)
            mlflow.log_metric("accuracy", 0.5 + step * 0.1, step=step)
        mlflow.log_metric("nan_metric", float("nan"), step=0)
        with mlflow.start_run(run_name="child", nested=True):
            mlflow.log_param("lr", 0.01)
            mlflow.log_metric("loss", 0.3, step=0)

    # Run 2: finished, metrics logged without a step (every point has step 0).
    with mlflow.start_run(run_name="no-steps"):
        mlflow.log_param("seed", 7)
        for value in (3.0, 2.0, 1.0):
            mlflow.log_metric("loss", value)
            time.sleep(0.01)

    # Run 3: failed.
    try:
        with mlflow.start_run(run_name="broken"):
            mlflow.log_metric("loss", 9.0, step=0)
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    # Run 4: deleted.
    with mlflow.start_run(run_name="gone") as gone:
        mlflow.log_metric("loss", 1.0, step=0)
    client.delete_run(gone.info.run_id)

    # Run 5: left running (started, never ended).
    live = client.create_run(
        client.get_experiment_by_name("resnet").experiment_id, run_name="live"
    )
    client.log_metric(live.info.run_id, "loss", 4.0, step=0)

    # A second experiment, deleted.
    old = client.create_experiment("old")
    client.create_run(old, run_name="old-run")
    client.delete_experiment(old)


def scrub_file_store(root: Path) -> None:
    """Drop machine-specific values and the empty artifact directories."""
    import shutil

    for path in sorted(root.rglob("*")):
        if path.name == "artifacts" and path.is_dir():
            shutil.rmtree(path)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".yaml"):
            text = path.read_text()
            text = text.replace(str(root), "/mlruns")
            text = "".join(
                "user_id: user\n" if line.startswith("user_id:") else line
                for line in text.splitlines(keepends=True)
            )
            path.write_text(text)
        elif path.parent.name == "tags":
            if path.name.startswith("mlflow.source.git."):
                path.unlink()
            elif path.name == "mlflow.user":
                path.write_text("user")
            elif path.name == "mlflow.source.name":
                path.write_text("train.py")


KEEP_TABLES = ("alembic_version", "experiments", "runs", "metrics", "params", "tags")


def scrub_sqlite(db: Path, out: Path) -> None:
    """Drop machine-specific values and the tables the importer never reads, then VACUUM."""
    import sqlite3

    con = sqlite3.connect(db)
    con.execute("UPDATE experiments SET artifact_location = 'file:///mlruns/' || experiment_id")
    con.execute("UPDATE runs SET artifact_uri = 'file:///mlruns/artifacts', user_id = 'user', source_name = 'train.py'")
    con.execute("DELETE FROM tags WHERE key LIKE 'mlflow.source.git.%'")
    con.execute("UPDATE tags SET value = 'user' WHERE key = 'mlflow.user'")
    con.execute("UPDATE tags SET value = 'train.py' WHERE key = 'mlflow.source.name'")
    con.commit()
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        if name not in KEEP_TABLES and not name.startswith("sqlite_"):
            con.execute(f'DROP TABLE "{name}"')
    con.commit()
    con.execute("VACUUM")
    con.close()


def main() -> None:
    out = Path(sys.argv[1]).resolve()
    populate(f"file:{out / 'filestore' / 'mlruns'}")
    scrub_file_store(out / "filestore" / "mlruns")
    (out / "sqlite").mkdir(parents=True, exist_ok=True)
    populate(f"sqlite:///{out / 'sqlite' / 'mlflow.db'}")
    scrub_sqlite(out / "sqlite" / "mlflow.db", out)


if __name__ == "__main__":
    main()
