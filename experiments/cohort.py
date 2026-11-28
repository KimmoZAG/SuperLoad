"""Rebuild the published comparison cohort from the Mooncake trace."""

import hashlib
import json
from pathlib import Path

from superload.dataset.prefix import build_plan


EXCLUSIONS = Path(__file__).with_name("cohort-exclusions.json")
ROLES = ("small", "large")
SOURCE_FIELDS = ("schema", "trace_sha256", "trace_url", "requests", "source_windows", "warmup_windows", "page_bytes")


def plan_bytes(plan):
    return (json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def select_cohort(source, manifest):
    """Remove earlier time windows and compact the remaining corpus page IDs."""
    if manifest["schema"] != 1:
        raise ValueError("unsupported cohort exclusion schema")
    # The historical 64-pair plan was emitted as a single JSON line with CRLF.
    source_payload = (json.dumps(source) + "\r\n").encode("utf-8")
    source_digest = hashlib.sha256(source_payload).hexdigest()
    if source_digest != manifest["source"]["plan_sha256"]:
        raise ValueError(f"candidate plan SHA-256 mismatch: {source_digest}")

    pairs = {pair["timestamp_ms"]: pair for pair in source["pairs"]}
    if len(pairs) != len(source["pairs"]):
        raise ValueError("candidate plan contains duplicate timestamps")
    excluded = set()
    for batch in manifest["excluded"]:
        timestamps = set(batch["timestamps_ms"])
        if len(timestamps) != len(batch["timestamps_ms"]) or timestamps & excluded:
            raise ValueError(f"duplicate excluded timestamp in {batch['id']}")
        if timestamps - pairs.keys():
            raise ValueError(f"excluded timestamps outside the candidate plan in {batch['id']}")
        excluded.update(timestamps)

    selected = [pairs[timestamp] for timestamp in sorted(pairs.keys() - excluded)]
    if not selected:
        raise ValueError("cohort exclusion left no request pairs")
    original_ids = sorted({page for pair in selected for role in ROLES for page in pair[role]})
    compact = {page: index for index, page in enumerate(original_ids)}
    return {
        **{field: source[field] for field in SOURCE_FIELDS},
        "workload": "tool-agent-comparison",
        "cohort": {
            "source_plan_sha256": source_digest,
            "excluded_plan_sha256": [batch["plan_sha256"] for batch in manifest["excluded"]],
            "selection": "all remaining timestamp-disjoint windows from the fixed 64-pair replay plan",
        },
        "file_page_hashes": [source["file_page_hashes"][page] for page in original_ids],
        "pairs": [
            {"timestamp_ms": pair["timestamp_ms"], **{role: [compact[page] for page in pair[role]] for role in ROLES}}
            for pair in selected
        ],
    }


def rebuild_cohort(trace, published, exclusions=EXCLUSIONS):
    manifest = json.loads(exclusions.read_bytes())
    reference_digest = hashlib.sha256(published).hexdigest()
    if reference_digest != manifest["published_plan_sha256"]:
        raise ValueError("published plan does not match the exclusion manifest")
    settings = manifest["source"]
    source = build_plan(trace, sample=settings["sample"], warmup=settings["warmup"], seed=settings["seed"])
    plan = select_cohort(source, manifest)
    if plan_bytes(plan) != published:
        raise ValueError("rebuilt cohort differs from the Release plan")
    return plan
