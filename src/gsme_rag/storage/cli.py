"""Local operator commands for storage and review"""

import argparse
import getpass
import hmac
from datetime import date
import json
import os
from pathlib import Path
import time

from gsme_rag.storage.config import from_env
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.pipeline import process_ingestion, process_index, reconcile, search_evidence
from gsme_rag.storage.providers import CohereEmbeddings, FakeEmbeddings
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.storage.evaluate import evaluate_cases


def _json(value):
    print(json.dumps(value, indent=2, default=str))


def main():
    # These commands are not mounted on the public parsing demo
    parser = argparse.ArgumentParser(
        description="Local GSME storage worker (no public ingestion/review API)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    submit = commands.add_parser("submit")
    submit.add_argument("file", type=Path)
    submit.add_argument("--tenant", required=True)
    submit.add_argument("--document-id")
    submit.add_argument("--schedule-kind", choices=("full", "shared"))
    status = commands.add_parser("status")
    status.add_argument("job_id")
    status.add_argument("--tenant", required=True)
    retry = commands.add_parser("retry")
    retry.add_argument("job_id")
    retry.add_argument("--tenant", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("version_id")
    inspect.add_argument("--tenant", required=True)
    inspect.add_argument("--limit", type=int, default=50)
    worker = commands.add_parser("worker-once")
    worker.add_argument("--provider", choices=("fake", "cohere"), default="fake")
    worker.add_argument("--tenant")
    worker.add_argument("--version-id")
    worker.add_argument("--job-id")
    worker.add_argument("--ingestion-only", action="store_true")
    worker.add_argument("--index-only", action="store_true")
    continuous = commands.add_parser("worker")
    continuous.add_argument("--provider", choices=("fake", "cohere"), default="cohere")
    continuous.add_argument("--tenant")
    continuous.add_argument("--version-id")
    continuous.add_argument("--poll-seconds", type=float, default=2.0)
    review = commands.add_parser("review")
    review.add_argument("--tenant", required=True)
    review.add_argument("--version-id", required=True)
    review.add_argument("--chunk-id", required=True)
    review.add_argument("--decision", choices=("approved", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--rationale", required=True)
    review.add_argument("--source-verified", action="store_true")
    publish = commands.add_parser("publish")
    publish.add_argument("--tenant", required=True)
    publish.add_argument("--document-id", required=True)
    publish.add_argument("--version-id", required=True)
    publish.add_argument("--provider", choices=("fake", "cohere"), default="cohere")
    search = commands.add_parser("search")
    search.add_argument("question")
    search.add_argument("--tenant", required=True)
    search.add_argument("--provider", choices=("fake", "cohere"), default="cohere")
    search.add_argument("--hybrid", action="store_true")
    schedule = commands.add_parser("schedules")
    schedule.add_argument("--tenant", required=True)
    schedule.add_argument("--kind", choices=("full", "shared"), required=True)
    schedule.add_argument("--technology-nm", type=int)
    schedule.add_argument("--flavor")
    schedule.add_argument("--tapeout-on-or-after", type=date.fromisoformat)
    schedule.add_argument("--document-id")
    cleanup = commands.add_parser("reconcile")
    cleanup.add_argument("--provider", choices=("fake", "cohere"), default="cohere")
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("cases", type=Path)
    evaluation.add_argument("--provider", choices=("fake", "cohere"), default="cohere")
    evaluation.add_argument("--hybrid", action="store_true")
    args = parser.parse_args()
    if (args.command in ("worker", "worker-once") and args.provider == "cohere"
            and not getattr(args, "ingestion_only", False)):
        if not args.tenant or not args.version_id:
            parser.error("Cohere indexing requires --tenant and --version-id")
    settings = from_env()
    store = EvidenceStore(settings.database_url)
    if args.command == "migrate":
        _json({"applied": store.migrate(Path(__file__).resolve().parents[3])})
        return
    if args.command == "submit":
        _json(
            store.submit(
                args.tenant, args.file, settings.data_dir, args.document_id, args.schedule_kind
            )
        )
        return
    if args.command == "status":
        _json(store.job_status(args.tenant, args.job_id))
        return
    if args.command == "retry":
        _json({"requeued": store.retry_job(args.tenant, args.job_id)})
        return
    if args.command == "inspect":
        _json(store.inspect_version(args.tenant, args.version_id, args.limit))
        return
    if args.command == "review":
        if args.reviewer not in {
            name.strip()
            for name in os.getenv("GSME_AUTHORIZED_REVIEWERS", "").split(",")
            if name.strip()
        }:
            raise SystemExit(
                "Reviewer is not on the local authorized list; do not approve production evidence here"
            )
        secret = os.getenv("GSME_REVIEW_AUTH_TOKEN")
        if not secret or not hmac.compare_digest(
            getpass.getpass("Local review authorization token: "), secret
        ):
            raise SystemExit("Review authorization denied")
        store.record_review(
            args.tenant,
            args.version_id,
            args.chunk_id,
            args.decision,
            args.reviewer,
            args.rationale,
            args.source_verified,
        )
        _json({"recorded": True})
        return
    provider = None
    if args.command in ("worker", "worker-once", "publish", "search", "evaluate", "reconcile"):
        provider_name = args.provider
        if args.command == "worker-once" and args.ingestion_only:
            provider_name = None
        provider = (
            (
                FakeEmbeddings(settings.dimension)
                if provider_name == "fake"
                else CohereEmbeddings(settings.dimension)
            )
            if provider_name
            else None
        )
    if args.command == "publish":
        _json(
            {
                "approved_candidates": store.publish(
                    args.tenant,
                    args.document_id,
                    args.version_id,
                    provider.model_version,
                    settings.collection + ("_synthetic" if args.provider == "fake" else ""),
                )
            }
        )
        return
    if args.command == "schedules":
        _json(
            store.schedules(
                args.tenant,
                args.kind,
                technology_nm=args.technology_nm,
                flavor=args.flavor,
                tapeout_on_or_after=args.tapeout_on_or_after,
                document_id=args.document_id,
            )
        )
        return
    if args.command in ("worker", "worker-once", "search", "reconcile", "evaluate"):
        vector = QdrantEvidence(
            settings.qdrant_url,
            settings.collection
            + ("_synthetic" if getattr(args, "provider", None) == "fake" else ""),
            settings.dimension,
            settings.hybrid_enabled,
        )
    if args.command == "search":
        _json(
            search_evidence(
                store, vector, provider, settings, args.tenant, args.question, hybrid=args.hybrid
            )
        )
        return
    if args.command == "reconcile":
        _json({"deleted": reconcile(store, vector, provider.model_version)})
        return
    if args.command == "evaluate":
        _json(evaluate_cases(store, vector, provider, settings, args.cases, args.hybrid))
        return

    def run_once():
        output = {}
        if not getattr(args, "index_only", False):
            output["ingestion"] = process_ingestion(
                store, settings, getattr(args, "job_id", None)
            )
        if provider and not getattr(args, "ingestion_only", False):
            output["index"] = process_index(
                store, vector, provider, settings,
                getattr(args, "tenant", None), getattr(args, "version_id", None)
            )
            output["reconciled"] = reconcile(store, vector, provider.model_version)
        return output

    if args.command == "worker-once":
        _json(run_once())
        return
    while True:
        result = run_once()
        if result.get("ingestion") or result.get("index", {}).get("indexed"):
            _json(result)
        else:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
