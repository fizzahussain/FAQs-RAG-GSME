import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from gsme_rag.chunking import chunk_markdown
from gsme_rag.parsing import parse_to_markdown


def _print_warnings(warnings) -> None:
    for warning in warnings:
        location = f" ({warning.location})" if warning.location else ""
        print(f"warning [{warning.code}]{location}: {warning.message}", file=sys.stderr)


def _parse_command(args: argparse.Namespace) -> int:
    result = parse_to_markdown(args.input)
    _print_warnings(result.warnings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.markdown, encoding="utf-8")
    print(f"Normalized {args.input.name} to {args.output}")
    return 0


def _chunk_command(args: argparse.Namespace) -> int:
    markdown = args.input.read_text(encoding="utf-8")
    result = chunk_markdown(markdown, args.input.name, args.max_chars)
    _print_warnings(result.warnings)
    payload = {
        "source_file": args.input.name,
        "max_chars": args.max_chars,
        "chunk_count": len(result.chunks),
        "chunks": [
            {**asdict(chunk), "character_count": chunk.character_count}
            for chunk in result.chunks
        ],
        "warnings": [asdict(warning) for warning in result.warnings],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Created {len(result.chunks)} chunks in {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsme-rag",
        description="Normalize files to Markdown, then chunk Markdown separately",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    parse = commands.add_parser("parse", help="Normalize a DOCX, PDF, or CSV to Markdown")
    parse.add_argument("input", type=Path)
    parse.add_argument("--output", "-o", required=True, type=Path)
    parse.set_defaults(handler=_parse_command)

    chunk = commands.add_parser("chunk", help="Create chunks from a Markdown file")
    chunk.add_argument("input", type=Path)
    chunk.add_argument("--output", "-o", required=True, type=Path)
    chunk.add_argument("--max-chars", type=int, default=1800)
    chunk.set_defaults(handler=_chunk_command)
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
