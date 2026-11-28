"""Replay the three published comparison profiles at the measured source revision."""

import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from report.comparison_data import PROFILES, read
from superload.cli import write_json

from .audit import RUN_ARTIFACTS, audit_results, check_sources, sha256


def git(root, *arguments, **options):
    return subprocess.run(["git", "-C", str(root), *arguments], **options)


def checkout_source(root, release, destination):
    commit = release.commit
    if not commit:
        raise ValueError("the release manifest must identify the measured source commit")
    revision = f"{commit}^{{commit}}"
    if git(root, "cat-file", "-e", revision, capture_output=True).returncode:
        git(root, "fetch", "--depth=1", "origin", "tag", release.tag, check=True)
    resolved = git(root, "rev-parse", "--verify", revision, check=True, capture_output=True, text=True)
    if resolved.stdout.strip() != commit:
        raise ValueError("unexpected source revision for the released benchmark")
    archive = git(root, "archive", "--format=zip", commit, "superload", check=True, capture_output=True).stdout
    with ZipFile(io.BytesIO(archive)) as package:
        package.extractall(destination)


def cann_versions():
    toolkit = os.environ.get("ASCEND_TOOLKIT_HOME") or os.environ.get("ASCEND_HOME_PATH")
    if not toolkit:
        return []
    directory = Path(toolkit)
    candidates = [directory / "version.info", *directory.glob("*/ascend_toolkit_install.info")]
    versions = set()
    for path in candidates:
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                key, equals, value = line.partition("=")
                if equals and key.strip().lower() in {"version", "runtime_version"} and value.strip():
                    versions.add(value.strip())
    return sorted(versions)


def capture_environment(output):
    if shutil.which("npu-smi"):
        with (output / "npu-smi.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(["npu-smi", "info"], stdout=log, stderr=subprocess.STDOUT, check=False)
        status = result.returncode
    else:
        status = None
    write_json({"system": platform.system(), "kernel": platform.release(), "machine": platform.machine(),
                "python": platform.python_version(), "cann_versions": cann_versions(), "npu_smi_status": status},
               output / "environment.json")


def run_logged(module, arguments, source, root, log):
    command = [sys.executable, "-B", "-m", module, *map(str, arguments)]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(source), str(root)))}
    print(f"Running {module}; log: {log}", flush=True)
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"{module} exited with status {result.returncode}; see {log}")
    return command


def reproduce(args, root, release, reference):
    if sys.platform != "linux":
        raise ValueError("hardware reproduction requires Linux and the Ascend runtime")
    output = args.output.resolve()
    corpus = args.corpus.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"choose an empty output directory: {output}")
    plan, recorded = read(reference)
    pair_count = len(plan["pairs"])
    repeats = args.repeat if args.repeat is not None else len(recorded["direct"]["trials"])
    subset = ["--pair-limit", args.pair_limit] if args.pair_limit and args.pair_limit < pair_count else []
    numa = ["--numa-node", args.numa_node] if args.numa_node is not None else []
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="superload-source-") as temporary:
        source = Path(temporary)
        checkout_source(root, release, source)
        receipt = reference / "implementation.sha256"
        check_sources(receipt, source)
        shutil.copyfile(receipt, output / receipt.name)
        shutil.copyfile(reference / "plan.json", output / "plan.json")
        capture_environment(output)
        commands = {}
        for profile, (io_mode, host) in PROFILES.items():
            runs = recorded[profile]
            arguments = [
                "prefix-compare", "--plan", output / "plan.json", "--corpus", corpus,
                "--io", io_mode, "--host-buffer", host, "--device", args.device, "--repeat", repeats,
                "--seed", runs["run_seed"], "--chunk-blocks", runs["chunk_blocks"],
                "--storage-label", args.storage_label, "--output", output / f"{profile}.json", *subset,
            ]
            if profile == "buffered":
                arguments.extend(numa)
            commands[profile] = run_logged("superload", arguments, source, root, output / f"{profile}.log")
        commands["verify-hbm"] = run_logged(
            "experiments",
            ["verify-hbm", "--results", output, "--corpus", corpus, "--device", args.device,
             "--output", output / "verification.json", *numa],
            source, root, output / "verify-hbm.log",
        )
        report = audit_results(output, source)
        write_json(report, output / "audit.json")
    write_json({
        "schema": 1, "reference_tag": release.tag, "reference_sha256": release.sha256,
        "source_commit": release.commit, "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(),
        "device": args.device, "numa_node": args.numa_node, "storage": args.storage_label,
        "repeat": repeats, "pair_limit": args.pair_limit, "commands": commands,
        "sha256": {name: sha256(output / name) for name in RUN_ARTIFACTS},
    }, output / "run-manifest.json")
    return report
