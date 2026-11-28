"""Audit a release or reproduce it on Ascend from the measured Git commit."""

import argparse
import subprocess
import tempfile
from pathlib import Path

from report.release_data import ResultsRelease
from superload.cli import positive, write_json

from .audit import audit_results


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "report" / "comparison-results-release.json"


def hardware_arguments(command):
    command.add_argument("--corpus", type=Path, required=True, help="directory for the two KV corpus layouts")
    command.add_argument("--device", type=int, default=0, help="logical Ascend device number")
    command.add_argument("--numa-node", type=int, help="CPU affinity for the pageable Buffered I/O pipeline")
    command.add_argument("--output", type=Path, required=True)


def parse_args():
    parser = argparse.ArgumentParser(prog="python3 -m experiments", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="recompute metrics and audit raw records without an NPU")
    data = audit.add_mutually_exclusive_group()
    data.add_argument("--archive", type=Path, help="offline copy of the pinned Release ZIP")
    data.add_argument("--results", type=Path, help="audit a local measurement directory")
    audit.add_argument("--source", type=Path, help="source tree to match against implementation checksums")
    audit.add_argument("--output", type=Path, help="write the audit and per-trial metrics as JSON")
    replay = commands.add_parser("reproduce", help="run the three profiles and full-page HBM checks on Ascend")
    hardware_arguments(replay)
    replay.add_argument("--archive", type=Path, help="offline copy of the pinned Release ZIP")
    replay.add_argument("--storage-label", default="unspecified", help="storage backend label for the run")
    replay.add_argument("--pair-limit", type=positive, help="take a deterministic subset for a smoke test")
    replay.add_argument("--repeat", type=positive, help="override the repetition count recorded in the release")
    verify = commands.add_parser("verify-hbm", help="rerun the full-page checks for measured minimum/maximum pairs")
    hardware_arguments(verify)
    verify.add_argument("--results", type=Path, required=True, help="measurement directory containing plan and results")
    cohort = commands.add_parser("rebuild-cohort", help="regenerate and verify the published plan from the Mooncake trace")
    cohort.add_argument("--trace", type=Path, required=True, help="Mooncake FAST'25 Tool & Agent trace JSONL")
    cohort.add_argument("--archive", type=Path, help="offline copy of the pinned Release ZIP")
    cohort.add_argument("--output", type=Path, required=True, help="write the verified plan as JSON")
    return parser.parse_args()


def print_audit(report):
    verification = report["hbm_verification"]
    print(f"Audited {report['timed_windows']} timed windows and {verification['windows']} full HBM checks "
          f"({verification['pages']} pages). SuperLoad leads in {report['wins']}/{len(report['comparisons'])} metrics.")


def main():
    args = parse_args()
    if args.command == "rebuild-cohort":
        from .cohort import rebuild_cohort

        release = ResultsRelease.read(RELEASE)
        with release.unpack(ROOT / "build" / "results", archive=args.archive) as reference:
            plan = rebuild_cohort(args.trace, (reference / "plan.json").read_bytes())
        write_json(plan, args.output)
        print(f"Rebuilt {len(plan['pairs'])} request pairs; plan bytes match {release.tag}.")
        return 0
    if args.command == "verify-hbm":
        from .verify_hbm import verify_hbm

        write_json(verify_hbm(args.results, args.corpus, args.device, args.numa_node), args.output)
        return 0
    if args.command == "audit" and args.results is not None:
        report = audit_results(args.results, args.source or ROOT)
    else:
        release = ResultsRelease.read(RELEASE)
        with release.unpack(ROOT / "build" / "results", archive=args.archive) as reference:
            if args.command == "audit":
                if args.source is not None:
                    report = audit_results(reference, args.source)
                else:
                    from .reproduce import checkout_source

                    with tempfile.TemporaryDirectory(prefix="superload-source-") as temporary:
                        source = Path(temporary)
                        checkout_source(ROOT, release, source)
                        report = audit_results(reference, source)
            else:
                from .reproduce import reproduce

                report = reproduce(args, ROOT, release, reference)
    if args.command == "audit" and args.output is not None:
        write_json(report, args.output)
    print_audit(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
