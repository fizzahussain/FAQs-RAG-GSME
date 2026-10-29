import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from gsme_rag.chunking import chunk_markdown, compare_strategies
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.models import DocumentMetadata, chunk_to_record


def _print_warnings(warnings) -> None:
    for warning in warnings:
        location = f" ({warning.location})" if warning.location else ""
        print(f"warning [{warning.code}]{location}: {warning.message}", file=sys.stderr)


def _parse_command(args: argparse.Namespace) -> int:
    result = parse_to_markdown(args.input, schedule_kind=args.schedule_kind)
    _print_warnings(result.warnings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.markdown, encoding="utf-8")
    sidecar = args.output.with_suffix(".metadata.json")
    sidecar.write_text(json.dumps(asdict(result.metadata), indent=2), encoding="utf-8")
    review = args.output.with_suffix(".review.json")
    review.write_text(json.dumps({
        "strategy_used": result.strategy_used,
        "metadata": asdict(result.metadata),
        "warnings": [asdict(item) for item in result.warnings],
        "evidence_diagnostics": [asdict(item) for item in result.evidence_diagnostics],
    }, indent=2), encoding="utf-8")
    print(f"Normalized {args.input.name} to {args.output}")
    print(f"Source metadata written to {sidecar}")
    print(f"Warnings and source diagnostics written to {review}")
    return 0


def _chunk_command(args: argparse.Namespace) -> int:
    markdown = args.input.read_text(encoding="utf-8")
    sidecar = args.metadata or args.input.with_suffix(".metadata.json")
    if args.metadata and not sidecar.is_file():
        raise ValueError("The explicitly requested metadata sidecar does not exist")
    metadata = DocumentMetadata(**json.loads(sidecar.read_text(encoding="utf-8"))) if sidecar.exists() else None
    result = chunk_markdown(
        markdown, metadata.source_file if metadata else args.input.name,
        args.max_chars, args.strategy, args.overlap_chars,
        metadata, args.semantic_schedule_rows,
    )
    _print_warnings(result.warnings)
    payload = {
        "source_file": metadata.source_file if metadata else args.input.name,
        "max_chars": args.max_chars,
        "strategy": args.strategy,
        "overlap_chars": args.overlap_chars if args.strategy in {"auto", "hybrid"} else 0,
        "chunk_count": len(result.chunks),
        "content_route": result.content_route,
        "chunks": [chunk_to_record(chunk) for chunk in result.chunks],
        "warnings": [asdict(warning) for warning in result.warnings],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Created {len(result.chunks)} chunks in {args.output}")
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    markdown = args.input.read_text(encoding="utf-8")
    cases = json.loads(args.cases.read_text(encoding="utf-8")) if args.cases else []
    if not isinstance(cases, list):
        raise ValueError("cases file must contain a JSON list")
    result = compare_strategies(
        markdown, cases, args.input.name, args.max_chars, args.overlap_chars
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Compared four strategies in {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsme-rag",
        description="Normalize files to Markdown, then chunk Markdown separately",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    parse = commands.add_parser("parse", help="Normalize a DOCX, PDF, CSV, or image to Markdown")
    parse.add_argument("input", type=Path)
    parse.add_argument("--output", "-o", required=True, type=Path)
    parse.add_argument("--schedule-kind", choices=["auto", "full", "shared"], default="auto")
    parse.set_defaults(handler=_parse_command)

    chunk = commands.add_parser("chunk", help="Create chunks from a Markdown file")
    chunk.add_argument("input", type=Path)
    chunk.add_argument("--output", "-o", required=True, type=Path)
    chunk.add_argument("--max-chars", type=int, default=1800)
    chunk.add_argument(
        "--strategy", choices=["auto", "fixed_size", "layout_aware", "hybrid", "paragraph_aware"],
        default="auto",
    )
    chunk.add_argument("--overlap-chars", type=int, default=120)
    chunk.add_argument("--metadata", type=Path, help="Parse metadata JSON; defaults to a sidecar beside the Markdown")
    chunk.add_argument("--semantic-schedule-rows", action="store_true")
    chunk.set_defaults(handler=_chunk_command)

    compare = commands.add_parser("compare", help="Compare four chunking strategies")
    compare.add_argument("input", type=Path)
    compare.add_argument("--output", "-o", required=True, type=Path)
    compare.add_argument("--cases", type=Path, help="JSON list of question/evidence pairs")
    compare.add_argument("--max-chars", type=int, default=1800)
    compare.add_argument("--overlap-chars", type=int, default=120)
    compare.set_defaults(handler=_compare_command)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        status_code = args.handler(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
        return
    raise SystemExit(status_code)


if __name__ == "__main__":
    main()
