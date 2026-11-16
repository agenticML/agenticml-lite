"""Package 24.13: the Phase 6 restore-drill tooling in scripts/restore_drill (no AWS, no network)."""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRILL = ROOT / "scripts" / "restore_drill"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, DRILL / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mk_td = _load("mk_td")
check_db = _load("check_db")
copy_versions_at = _load("copy_versions_at")

# A production task definition with dummy identifiers.
PROD_DB_HOST = "agenticml-hosted.dummyhash.us-east-2.rds.amazonaws.com"
PROD_BUCKET = "agenticml-hosted-objectsbucket-dummy"
PROD_FS = "fs-prod-dummy"
PROD_AP = "ap-prod-dummy"
PROD_HOSTNAME = "app.example.test"
PROD_TG = "targetgroup/prod-dummy/abc"
DRILL_HOST = "agenticml-restore-drill.otherhash.us-east-2.rds.amazonaws.com"
DRILL_BUCKET = "agenticml-restore-drill-20261116-1"
IMAGE = "123456789012.dkr.ecr.us-east-2.amazonaws.com/agenticml/lite-server:0123abc456def-arm64"
SECRET = {"name": "PGPASSWORD", "valueFrom": "arn:aws:secretsmanager:us-east-2:123456789012:secret:dummy:password::"}


@pytest.fixture
def prod():
    return {
        "family": "agenticml-hosted", "revision": 9, "cpu": "512", "memory": "1024",
        "taskRoleArn": "arn:aws:iam::123456789012:role/dummy-task",
        "executionRoleArn": "arn:aws:iam::123456789012:role/dummy-execution",
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "volumes": [{"name": "data", "efsVolumeConfiguration": {
            "fileSystemId": PROD_FS, "rootDirectory": "/", "transitEncryption": "ENABLED",
            "authorizationConfig": {"accessPointId": PROD_AP, "iam": "ENABLED"}}}],
        "containerDefinitions": [{
            "name": "server", "image": IMAGE, "essential": True,
            "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
            "environment": [
                {"name": "AGENTICML_SERVER_DATABASE_URL",
                 "value": "postgresql://agenticml@%s:5432/agenticml?sslmode=verify-full&sslrootcert=/etc/agenticml/rds-ca.pem"
                          % PROD_DB_HOST},
                {"name": "AGENTICML_SERVER_OBJECTS", "value": "s3://%s/hosted" % PROD_BUCKET},
                {"name": "AGENTICML_SERVER_PUBLIC_URL", "value": "https://%s" % PROD_HOSTNAME},
                {"name": "AGENTICML_SERVER_FIREBASE_PROJECT", "value": "dummy-project"},
                {"name": "AGENTICML_SERVER_FIREBASE_API_KEY", "value": "dummy-key"},
                {"name": "AGENTICML_SERVER_OPEN_SIGNUP", "value": "true"},
                {"name": "AGENTICML_SERVER_LIMITS", "value": "/etc/agenticml/limits.json"},
                {"name": "AGENTICML_SERVER_TRUSTED_PROXY", "value": "10.0.0.0/24"},
                {"name": "AWS_REGION", "value": "us-east-2"}],
            "secrets": [SECRET],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": "/ecs/dummy", "awslogs-region": "us-east-2", "awslogs-stream-prefix": "server"}}}],
    }


def drill_env(**extra):
    env = {"DFS": "fs-drill-dummy", "AP_INSPECT": "ap-drill-inspect", "AP_DATA": "ap-drill-data",
           "AP_BENCH": "ap-drill-bench", "DB_HOST": DRILL_HOST, "TEST_B": DRILL_BUCKET,
           "BENCH_COMMIT": "0123abc"}
    env.update(extra)
    return env


def build(kind, prod, env=None, prod_flag=False):
    return mk_td.build(kind, prod, drill_env() if env is None else env, prod_flag,
                       script_loader=lambda commit: b"print('bench')\n")[0]


def walk_text(td):
    return json.dumps(td)


# ------------------------------------------------------------------ mk_td: building

@pytest.mark.parametrize("kind,family", [
    ("inspect", "agenticml-restore-drill-inspect"), ("dbcheck", "agenticml-restore-drill-dbcheck"),
    ("server", "agenticml-restore-drill-server"), ("bench", "agenticml-restore-drill-bench")])
def test_each_definition_is_built_and_tagged(prod, kind, family):
    td = build(kind, prod)
    assert td["family"] == family
    assert {"key": "Project", "value": "agenticml-restore-drill"} in td["tags"]
    assert td["executionRoleArn"] == prod["executionRoleArn"]
    assert td["requiresCompatibilities"] == ["FARGATE"] and td["networkMode"] == "awsvpc"
    assert td["containerDefinitions"][0]["image"] == IMAGE
    assert "enableExecuteCommand" not in walk_text(td)
    for ident in (PROD_DB_HOST, PROD_BUCKET, PROD_FS, PROD_AP, PROD_HOSTNAME):
        assert ident not in walk_text(td)
    container = td["containerDefinitions"][0]
    assert "portMappings" not in container
    assert container["logConfiguration"]["options"]["awslogs-group"] == "/ecs/dummy"
    assert container["logConfiguration"]["options"]["awslogs-stream-prefix"].startswith("restore-drill")


def test_server_definition_uses_only_drill_resources(prod):
    td = build("server", prod)
    container = td["containerDefinitions"][0]
    env = {e["name"]: e["value"] for e in container["environment"]}
    assert env["AGENTICML_SERVER_DATABASE_URL"] == (
        "postgresql://agenticml@%s:5432/agenticml?sslmode=verify-full&sslrootcert=/etc/agenticml/rds-ca.pem"
        % DRILL_HOST)
    assert env["AGENTICML_SERVER_OBJECTS"] == "s3://%s/hosted" % DRILL_BUCKET
    assert env["AGENTICML_SERVER_PUBLIC_URL"] == "http://127.0.0.1:8080"
    assert env["AGENTICML_SERVER_OPEN_SIGNUP"] == "false"
    assert not [name for name in env if "FIREBASE" in name]
    assert container["entryPoint"] == ["sh", "-c", (DRILL / "drill-run.sh").read_text()]
    assert container["secrets"] == [SECRET]
    assert td["taskRoleArn"] == prod["taskRoleArn"]
    efs = td["volumes"][0]["efsVolumeConfiguration"]
    assert efs["fileSystemId"] == "fs-drill-dummy" and efs["authorizationConfig"]["accessPointId"] == "ap-drill-data"


def test_inspect_and_dbcheck_have_no_task_role(prod):
    for kind in ("inspect", "dbcheck"):
        assert "taskRoleArn" not in build(kind, prod)
    inspect = build("inspect", prod)
    assert inspect["containerDefinitions"][0]["mountPoints"][0]["readOnly"] is True
    dbcheck = build("dbcheck", prod)["containerDefinitions"][0]
    assert dbcheck["secrets"] == [SECRET]
    assert [e["value"] for e in dbcheck["environment"]] == [
        "postgresql://agenticml@%s:5432/agenticml?sslmode=verify-full&sslrootcert=/etc/agenticml/rds-ca.pem"
        % DRILL_HOST]


def test_dbcheck_prod_is_the_only_definition_that_may_name_the_prod_database(prod):
    td = build("dbcheck", prod, drill_env(DB_HOST=""), prod_flag=True)
    assert td["family"] == "agenticml-restore-drill-dbcheck-prod"
    assert PROD_DB_HOST in walk_text(td)
    with pytest.raises(mk_td.GuardError, match="--prod"):
        build("dbcheck", prod, drill_env(DB_HOST=PROD_DB_HOST))


# ------------------------------------------------------------------ mk_td: refusals

def test_refuses_production_database_host(prod):
    with pytest.raises(mk_td.GuardError, match="DB_HOST"):
        build("server", prod, drill_env(DB_HOST=PROD_DB_HOST))
    with pytest.raises(mk_td.GuardError, match="DB_HOST"):
        build("dbcheck", prod, drill_env(DB_HOST="somewhere-else.example"))


def test_refuses_production_bucket(prod):
    with pytest.raises(mk_td.GuardError, match="TEST_B"):
        build("server", prod, drill_env(TEST_B=PROD_BUCKET))


@pytest.mark.parametrize("kind,variable", [("inspect", "AP_INSPECT"), ("server", "AP_DATA"), ("bench", "AP_BENCH")])
def test_refuses_production_access_point_naming_the_field(prod, kind, variable):
    with pytest.raises(mk_td.GuardError) as info:
        build(kind, prod, drill_env(**{variable: PROD_AP}))
    assert "production access point" in str(info.value)
    assert "volumes[0].efsVolumeConfiguration.authorizationConfig.accessPointId" in str(info.value)


@pytest.mark.parametrize("kind", ["inspect", "server", "bench"])
def test_refuses_production_filesystem_naming_the_field(prod, kind):
    with pytest.raises(mk_td.GuardError) as info:
        build(kind, prod, drill_env(DFS=PROD_FS))
    assert "production filesystem" in str(info.value)
    assert "volumes[0].efsVolumeConfiguration.fileSystemId" in str(info.value)


def test_guard_refuses_ecs_exec_ports_dns_and_target_group(prod):
    ids = mk_td.prod_identifiers(prod, PROD_TG)
    assert ids["production hostname"] == PROD_HOSTNAME
    good = build("server", prod)
    mk_td.check_guards(good, "server", ids)

    exec_flag = copy.deepcopy(good)
    exec_flag["containerDefinitions"][0]["enableExecuteCommand"] = True
    with pytest.raises(mk_td.GuardError, match=r"containerDefinitions\[0\]\.enableExecuteCommand.*ECS Exec"):
        mk_td.check_guards(exec_flag, "server", ids)

    port = copy.deepcopy(good)
    port["containerDefinitions"][0]["portMappings"] = [{"containerPort": 8080}]
    with pytest.raises(mk_td.GuardError, match=r"containerDefinitions\[0\]\.portMappings"):
        mk_td.check_guards(port, "server", ids)

    dns = copy.deepcopy(good)
    dns["containerDefinitions"][0]["environment"][0]["value"] = "https://" + PROD_HOSTNAME
    with pytest.raises(mk_td.GuardError, match="production hostname"):
        mk_td.check_guards(dns, "server", ids)

    tg = copy.deepcopy(good)
    tg["tags"].append({"key": "tg", "value": "arn:aws:elasticloadbalancing:x:y:" + PROD_TG})
    with pytest.raises(mk_td.GuardError, match="extra production identifier 1"):
        mk_td.check_guards(tg, "server", ids)

    public = copy.deepcopy(good)
    for item in public["containerDefinitions"][0]["environment"]:
        if item["name"] == "AGENTICML_SERVER_PUBLIC_URL":
            item["value"] = "https://drill.example.test"
    with pytest.raises(mk_td.GuardError, match="loopback"):
        mk_td.check_guards(public, "server", ids)

    firebase = copy.deepcopy(good)
    firebase["containerDefinitions"][0]["environment"].append(
        {"name": "AGENTICML_SERVER_FIREBASE_PROJECT", "value": "x"})
    with pytest.raises(mk_td.GuardError, match="Firebase"):
        mk_td.check_guards(firebase, "server", ids)


def test_main_exits_2_names_the_problem_and_writes_nothing(prod, tmp_path, capsys):
    prod_file = tmp_path / "prod.json"
    prod_file.write_text(json.dumps({"taskDefinition": prod}))
    out = tmp_path / "out.json"
    code = mk_td.main(["server", str(prod_file), str(out)], drill_env(DFS=PROD_FS))
    assert code == 2 and not out.exists()
    assert "production filesystem" in capsys.readouterr().err
    code = mk_td.main(["server", str(prod_file), str(out)], drill_env(DB_HOST=""))
    assert code == 1 and "DB_HOST" in capsys.readouterr().err
    assert mk_td.main(["nope", str(prod_file), str(out)], {}) == 64
    assert mk_td.main(["server", str(prod_file), str(out)], drill_env()) == 0
    assert json.loads(out.read_text())["family"] == "agenticml-restore-drill-server"


# ------------------------------------------------------------------ mk_td: bench

def test_bench_has_no_role_environment_secrets_or_ports(prod):
    td = build("bench", prod)
    container = td["containerDefinitions"][0]
    assert "taskRoleArn" not in td
    assert container["environment"] == [] and container["secrets"] == []
    assert "portMappings" not in container
    assert td["cpu"] == "4096" and td["memory"] == "8192"
    assert td["volumes"][0]["efsVolumeConfiguration"]["authorizationConfig"]["accessPointId"] == "ap-drill-bench"


def test_bench_embeds_the_pinned_script_with_its_checksum(prod):
    script = b"print('bench')\n"
    seen = []

    def loader(commit):
        seen.append(commit)
        return script

    td, _ = mk_td.build("bench", prod, drill_env(BENCH_COMMIT=""), False, script_loader=loader)
    assert seen == ["0123abc456def"]  # the commit in the image tag
    command = td["containerDefinitions"][0]["command"][0]
    sha = hashlib.sha256(script).hexdigest()
    assert "SHA=%s\n" % sha in command
    payload = command.split("echo '", 1)[1].split("'", 1)[0]
    assert gzip.decompress(base64.b64decode(payload)) == script
    assert td["containerDefinitions"][0]["entryPoint"] == ["sh", "-c"]
    mk_td.build("bench", prod, drill_env(BENCH_COMMIT="pinned1"), False, script_loader=loader)
    assert seen[-1] == "pinned1"


def test_bench_commit_needs_a_pin_when_the_image_tag_has_none(prod):
    prod["containerDefinitions"][0]["image"] = "repo/name:latest"
    with pytest.raises(ValueError, match="BENCH_COMMIT"):
        mk_td.build("bench", prod, drill_env(BENCH_COMMIT=""), False, script_loader=lambda c: b"x")


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_bench_script_comes_from_git_not_the_working_tree():
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"], capture_output=True)
    if head.returncode != 0:
        pytest.skip("no HEAD commit")
    shown = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:scripts/bench_server.py"], capture_output=True)
    if shown.returncode != 0:
        pytest.skip("bench_server.py is not in HEAD")
    assert mk_td.bench_script_at("HEAD") == shown.stdout
    with pytest.raises(ValueError, match="git show"):
        mk_td.bench_script_at("0000000000000000000000000000000000000000")


def test_bench_template_keeps_the_database_on_local_disk_and_keeps_the_exit_99_guard():
    command, _ = mk_td.bench_command(b"print('x')\n", "abc1234", 3)
    # The task-level guard still comes first and still refuses a database URL in the environment.
    guard = command.index('[ -n "${AGENTICML_SERVER_DATABASE_URL-}" ]')
    assert guard < command.index("bench_server.py") and "exit 99" in command
    assert "LOCALDB=/tmp/bench-db" in command and 'mkdir -p "$LOCALDB"' in command
    assert "## db sqlite on local disk (/tmp), run files and objects on EFS" in command
    # The URL is set for the one command only (no export), under /tmp, one file per run.
    line = next(l.strip() for l in command.splitlines() if "timeout 900 python" in l)
    assert line.startswith('AGENTICML_SERVER_DATABASE_URL="sqlite:///$LOCALDB/c$n-r$r.db" timeout 900 python ')
    assert "export AGENTICML_SERVER_DATABASE_URL" not in command
    assert '--data "$B/c$n-r$r"' in line  # run files and objects stay on the EFS directory
    assert 'rm -f -- "$LOCALDB/c$n-r$r.db"' in command


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("sha256sum") is None, reason="needs bash")
def test_bench_container_script_refuses_production_env_and_a_tampered_script(tmp_path):
    script = b"print('hi')\n"
    command, sha = mk_td.bench_command(script, "abc1234", 1)
    base_env = {"PATH": os.environ["PATH"]}
    prod_env = dict(base_env, AGENTICML_SERVER_OBJECTS="s3://x/y")
    done = subprocess.run(["bash", "-c", command], env=prod_env, capture_output=True, text=True)
    assert done.returncode == 99 and "production environment" in done.stdout
    tampered = command.replace("SHA=" + sha, "SHA=" + "0" * 64)
    done = subprocess.run(["bash", "-c", tampered], env=base_env, capture_output=True, text=True)
    assert done.returncode == 98 and "checksum mismatch" in done.stdout


# ------------------------------------------------------------------ version selection at T

def version(key, vid, when, size=10):
    return {"Key": key, "VersionId": vid, "LastModified": when, "Size": size}


def marker(key, vid, when):
    return {"Key": key, "VersionId": vid, "LastModified": when}


T = "2026-11-16T12:00:00+00:00"


def test_selects_the_latest_version_at_or_before_t():
    versions = [version("hosted/w/a", "v1", "2026-11-16T10:00:00+00:00"),
                version("hosted/w/a", "v2", "2026-11-16T11:00:00+00:00"),
                version("hosted/w/a", "v3", "2026-11-16T13:00:00+00:00")]
    selected, hidden = copy_versions_at.select_versions(versions, [], T)
    assert selected["hosted/w/a"]["VersionId"] == "v2" and hidden == []


def test_a_key_created_after_t_is_absent():
    selected, hidden = copy_versions_at.select_versions(
        [version("hosted/w/new", "v1", "2026-11-16T12:00:01+00:00")], [], T)
    assert selected == {} and hidden == []


def test_a_version_at_exactly_t_is_included():
    selected, _ = copy_versions_at.select_versions([version("hosted/w/a", "v1", T)], [], T)
    assert list(selected) == ["hosted/w/a"]


def test_delete_marker_before_t_hides_the_key_and_after_t_does_not():
    versions = [version("hosted/w/a", "v1", "2026-11-16T09:00:00+00:00")]
    selected, hidden = copy_versions_at.select_versions(
        versions, [marker("hosted/w/a", "m1", "2026-11-16T10:00:00+00:00")], T)
    assert selected == {} and hidden == ["hosted/w/a"]
    selected, hidden = copy_versions_at.select_versions(
        versions, [marker("hosted/w/a", "m1", "2026-11-16T12:30:00+00:00")], T)
    assert selected["hosted/w/a"]["VersionId"] == "v1" and hidden == []


def test_a_version_restored_after_a_marker_before_t_is_selected():
    versions = [version("hosted/w/a", "v1", "2026-11-16T08:00:00+00:00"),
                version("hosted/w/a", "v2", "2026-11-16T11:00:00+00:00")]
    selected, hidden = copy_versions_at.select_versions(
        versions, [marker("hosted/w/a", "m1", "2026-11-16T10:00:00+00:00")], T)
    assert selected["hosted/w/a"]["VersionId"] == "v2" and hidden == []


def test_a_tie_between_version_and_marker_hides_the_key():
    when = "2026-11-16T10:00:00+00:00"
    selected, hidden = copy_versions_at.select_versions(
        [version("hosted/w/a", "v1", when)], [marker("hosted/w/a", "m1", when)], T)
    assert selected == {} and hidden == ["hosted/w/a"]


def test_time_formats_and_a_naive_time_is_rejected():
    assert copy_versions_at.parse_time("2026-11-16T12:00:00Z") == copy_versions_at.parse_time(T)
    assert copy_versions_at.parse_time("2026-11-16T12:00:00.5+00:00").microsecond == 500000
    assert copy_versions_at.parse_time("2026-11-16T17:30:00+05:30") == copy_versions_at.parse_time(T)
    with pytest.raises(ValueError, match="offset"):
        copy_versions_at.parse_time("2026-11-16T12:00:00")


# ------------------------------------------------------------------ copy_versions_at wrapper (fake aws)

SHA_A = hashlib.sha256(b"a").hexdigest()
SHA_B = hashlib.sha256(b"b").hexdigest()


class FakeAws:
    """Records s3api calls; serves one listing and head-object answers."""

    def __init__(self, versions, markers=(), corrupt=False):
        self.listing = {"Versions": versions, "DeleteMarkers": list(markers)}
        self.calls = []
        self.corrupt = corrupt
        self.sizes = {v["Key"]: v["Size"] for v in versions}

    def __call__(self, args):
        assert args[0] == "s3api"
        command, rest = args[1], args[2:]
        self.calls.append((command, rest))
        if command == "list-object-versions":
            return json.dumps(self.listing)
        if command == "copy-object":
            return "{}"
        if command == "head-object":
            key = rest[rest.index("--key") + 1]
            digest = key.rsplit("/", 1)[1]
            checksum = base64.b64encode(bytes.fromhex(digest)).decode()
            return json.dumps({"ContentLength": self.sizes[key] + (1 if self.corrupt else 0),
                               "ChecksumSHA256": checksum})
        raise AssertionError(command)


def listing():
    return ([version("hosted/w/objects/aa/" + SHA_A, "va", "2026-11-16T10:00:00+00:00", 1),
             version("hosted/w/objects/bb/" + SHA_B, "vb", "2026-11-16T10:00:00+00:00", 1)],
            [marker("hosted/w/objects/bb/" + SHA_B, "mb", "2026-11-16T11:00:00+00:00")])


def test_wrapper_dry_run_writes_nothing(capsys):
    fake = FakeAws(*listing())
    assert copy_versions_at.main([T, "src-bucket", DRILL_BUCKET, "--dry-run"], runner=fake) == 0
    assert [c for c, _ in fake.calls] == ["list-object-versions"]
    out = capsys.readouterr().out
    assert "would copy 1, hidden 1" in out and "hidden at T (delete marker)" in out


def test_wrapper_copies_only_into_the_destination_and_reads_the_source_version(capsys):
    fake = FakeAws(*listing())
    assert copy_versions_at.main([T, "src-bucket", DRILL_BUCKET], runner=fake) == 0
    commands = [c for c, _ in fake.calls]
    assert commands == ["list-object-versions", "copy-object", "head-object"]
    for command, args in fake.calls:
        if command == "list-object-versions":
            assert args[args.index("--bucket") + 1] == "src-bucket"
        else:
            assert args[args.index("--bucket") + 1] == DRILL_BUCKET
    copy_args = fake.calls[1][1]
    assert copy_args[copy_args.index("--copy-source") + 1] == "src-bucket/hosted/w/objects/aa/%s?versionId=va" % SHA_A
    assert "copied 1, hidden 1" in capsys.readouterr().out


def test_wrapper_stops_on_an_integrity_failure():
    fake = FakeAws(*listing(), corrupt=True)
    with pytest.raises(SystemExit, match="INTEGRITY FAILURE"):
        copy_versions_at.main([T, "src-bucket", DRILL_BUCKET], runner=fake)


def test_wrapper_checks_keys_that_are_not_digests_by_size_only(capsys):
    fake = FakeAws([version("hosted/w/other", "v", "2026-11-16T10:00:00+00:00", 3)])
    fake.sizes["hosted/w/other"] = 3

    def runner(args):
        if args[1] == "head-object":
            return json.dumps({"ContentLength": 3})
        return fake(args)

    assert copy_versions_at.main([T, "src-bucket", DRILL_BUCKET], runner=runner) == 0
    assert "size-only 1" in capsys.readouterr().out


def test_wrapper_refuses_a_destination_that_is_not_a_drill_bucket(capsys):
    fake = FakeAws(*listing())
    for dst in ("src-bucket", "agenticml-hosted-objectsbucket-dummy", "other"):
        assert copy_versions_at.main([T, "src-bucket", dst], runner=fake) == 2
    assert copy_versions_at.main([T, DRILL_BUCKET, DRILL_BUCKET], runner=fake) == 2
    assert fake.calls == []


def test_aws_cli_refuses_any_write_that_is_not_to_the_destination():
    seen = []
    cli = copy_versions_at.AwsCli(DRILL_BUCKET, runner=lambda args: seen.append(args) or "{}")
    for command in ("copy-object", "put-object", "delete-object", "delete-objects", "put-bucket-policy"):
        with pytest.raises(RuntimeError, match="not the destination"):
            cli.call(command, "--bucket", "src-bucket", "--key", "k")
        with pytest.raises(RuntimeError, match="not the destination"):
            cli.call(command, "--key", "k")
    assert seen == []
    cli.call("head-object", "--bucket", "src-bucket", "--key", "k")  # reads are fine
    cli.call("copy-object", "--bucket", DRILL_BUCKET, "--key", "k")
    assert len(seen) == 2


# ------------------------------------------------------------------ drill-run.sh guards (bash, fake env)

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def python_shim(tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python").symlink_to(sys.executable)
    return shim


def run_wrapper(python_shim, **env):
    full = {"PATH": "%s:%s" % (python_shim, os.environ["PATH"])}
    full.update(env)
    return subprocess.run(["bash", str(DRILL / "drill-run.sh")], env=full, capture_output=True, text=True,
                          timeout=60, cwd=str(python_shim))


GOOD = {
    "AGENTICML_SERVER_DATABASE_URL": "postgresql://agenticml@%s:5432/agenticml?sslmode=verify-full"
                                     "&sslrootcert=/etc/agenticml/rds-ca.pem" % DRILL_HOST,
    "AGENTICML_SERVER_OBJECTS": "s3://%s/hosted" % DRILL_BUCKET,
    "AGENTICML_SERVER_PUBLIC_URL": "http://127.0.0.1:8080",
}


@needs_bash
def test_drill_run_accepts_drill_values_and_starts_nothing_in_check_only_mode(python_shim):
    done = run_wrapper(python_shim, DRILL_CHECK_ONLY="1", **GOOD)
    assert done.returncode == 0 and "## guards ok" in done.stdout
    assert "## mount" not in done.stdout


@needs_bash
@pytest.mark.parametrize("name,value,message", [
    ("AGENTICML_SERVER_DATABASE_URL",
     "postgresql://agenticml@%s:5432/agenticml?sslmode=verify-full" % PROD_DB_HOST, "database host"),
    ("AGENTICML_SERVER_DATABASE_URL",
     "postgresql://agenticml@agenticml-restore-drill.x@%s:5432/agenticml" % PROD_DB_HOST, "database host"),
    ("AGENTICML_SERVER_DATABASE_URL",
     "postgresql://agenticml@%s:5432/agenticml?host=%s" % (DRILL_HOST, PROD_DB_HOST), "option"),
    ("AGENTICML_SERVER_DATABASE_URL", "", "database host"),
    ("AGENTICML_SERVER_OBJECTS", "s3://%s/hosted" % PROD_BUCKET, "bucket"),
    ("AGENTICML_SERVER_OBJECTS", "file:///data", "bucket"),
    ("AGENTICML_SERVER_OBJECTS", "s3://%s-x@%s/hosted" % ("agenticml-restore-drill", PROD_BUCKET), "bucket"),
    ("AGENTICML_SERVER_OBJECTS", "", "bucket"),
    ("AGENTICML_SERVER_PUBLIC_URL", "https://%s" % PROD_HOSTNAME, "loopback"),
    ("AGENTICML_SERVER_PUBLIC_URL", "http://127.0.0.1.example.test:8080", "loopback"),
    ("AGENTICML_SERVER_PUBLIC_URL", "", "loopback"),
])
def test_drill_run_refuses_production_looking_values_before_starting(python_shim, name, value, message):
    env = dict(GOOD)
    env[name] = value
    done = run_wrapper(python_shim, **env)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "REFUSING" in done.stdout and message in done.stdout
    for started in ("## guards ok", "## mount", "## healthy", "## status", "## done"):
        assert started not in done.stdout


@needs_bash
def test_drill_run_refuses_an_empty_environment(python_shim):
    done = run_wrapper(python_shim)
    assert done.returncode == 3 and done.stdout.count("REFUSING") == 3


@needs_bash
def test_drill_run_is_valid_posix_sh():
    for shell in ("bash", "sh"):
        assert subprocess.run([shell, "-n", str(DRILL / "drill-run.sh")]).returncode == 0


# ------------------------------------------------------------------ run_task.sh and common.sh (no aws calls)

@needs_bash
@pytest.mark.parametrize("args,env", [
    (["agenticml-hosted"], {}),
    (["some-other-family"], {}),
    ([], {}),
    (["agenticml-restore-drill-server"], {"DRILL_CLUSTER": "agenticml-hosted"}),
])
def test_run_task_refuses_other_clusters_and_families_before_any_aws_call(tmp_path, args, env):
    done = subprocess.run(["bash", str(DRILL / "run_task.sh"), *args], capture_output=True, text=True,
                          env=dict({"PATH": "/usr/bin:/bin", "DRILL_STATE_DIR": str(tmp_path / "state")}, **env))
    assert done.returncode == 2, done.stderr
    assert not (tmp_path / "state").exists()  # refused before touching the state directory


@needs_bash
def test_state_file_helpers(tmp_path):
    script = (
        '. "%s/common.sh"; drill_init; drill_set A one; drill_set B "two words"; drill_set A three; '
        'drill_need A B && echo needs-ok; drill_need MISSING || echo needs-fail; echo "$A|$B"'
    ) % DRILL
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "DRILL_STATE_DIR": str(tmp_path / "state")})
    assert done.returncode == 0, done.stderr
    assert "needs-ok" in done.stdout and "needs-fail" in done.stdout and "three|two words" in done.stdout
    state = (tmp_path / "state" / "state.env").read_text().splitlines()
    assert sorted(line.split("=")[0] for line in state) == ["export A", "export B"]
    again = subprocess.run(["bash", "-c", '. "%s/common.sh"; drill_init; echo "$A|$B"' % DRILL], capture_output=True,
                           text=True, env={"PATH": os.environ["PATH"], "DRILL_STATE_DIR": str(tmp_path / "state")})
    assert again.stdout.strip() == "three|two words"


# ------------------------------------------------------------------ check_db.py (fake connection)

class FakeDb:
    """Answers check_db queries; `fail` maps a substring of the SQL to an exception to raise."""

    def __init__(self, tables=None, fail=None):
        self.tables = set(check_db.COUNT_TABLES + ("schema_migrations",) if tables is None else tables)
        self.fail = fail or {}
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        for needle, exc in self.fail.items():
            if needle in sql:
                raise exc
        self._rows = self._answer(sql, params)
        return self

    def fetchall(self):
        return self._rows

    def _answer(self, sql, params):
        if "to_regclass" in sql:
            return [(params[0] in self.tables,)]
        if "rds.force_ssl" in sql:
            return [(None,)]
        if "max(version)" in sql:
            return [(9, 9)]
        if sql.startswith("select count(*)"):
            return [(3,)]
        return [("x",)]


def run_check(db):
    lines = []
    ok = check_db.run(db, out=lambda *parts: lines.append(" ".join(str(p) for p in parts)))
    return ok, lines


def test_check_db_all_required_queries_pass_and_force_ssl_is_never_fatal():
    db = FakeDb()
    ok, lines = run_check(db)
    assert ok
    assert "rds.force_ssl not visible to this role" in lines
    assert not any("show rds" in sql.lower() for sql in db.sql)  # SHOW would raise for the app role
    assert any(line.startswith("migrations") for line in lines)
    assert sum(line.startswith("count ") for line in lines) == len(check_db.COUNT_TABLES)


def test_check_db_prints_the_force_ssl_value_when_visible():
    db = FakeDb()
    original = db._answer
    db._answer = lambda sql, params: [("on",)] if "rds.force_ssl" in sql else original(sql, params)
    ok, lines = run_check(db)
    assert ok and "rds.force_ssl on" in lines


def test_check_db_optional_failure_does_not_stop_the_rest_or_the_verdict():
    db = FakeDb(fail={"rds.force_ssl": RuntimeError("unrecognized configuration parameter"),
                      "show server_version": RuntimeError("denied")})
    ok, lines = run_check(db)
    assert ok
    assert any(line.startswith("count runs") for line in lines)
    assert "rds.force_ssl not visible to this role" in lines


def test_check_db_required_failure_is_reported_later_queries_still_run_and_ok_is_withheld():
    db = FakeDb(fail={"pg_is_in_recovery": RuntimeError("boom")})
    ok, lines = run_check(db)
    assert not ok
    assert any(line.startswith("in_recovery ERROR") for line in lines)
    assert any(line.startswith("count objects") for line in lines)
    assert lines[-1].startswith("DRILL_DB_FAILED") and "in_recovery" in lines[-1]


def test_check_db_skips_and_reports_an_absent_table_and_fails():
    db = FakeDb(tables={"accounts", "workspaces", "memberships", "keys", "runs", "run_files", "schema_migrations"})
    ok, lines = run_check(db)
    assert not ok
    assert "table objects ABSENT" in lines
    assert not any("from objects" in sql for sql in db.sql)
    assert any(line.startswith("count run_files") for line in lines)


def test_check_db_tables_exist_in_the_current_schema():
    migrations = "\n".join(p.read_text() for p in (ROOT / "src/agenticml/server/db/migrations").iterdir()
                           if p.suffix in (".sql", ".py"))
    migrate = (ROOT / "src/agenticml/server/db/migrate.py").read_text()
    for table in check_db.COUNT_TABLES:
        assert "CREATE TABLE %s" % table in migrations, table
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in migrate
