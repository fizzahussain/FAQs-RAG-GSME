import argparse
from pathlib import Path
import sys

from gsme_parser.faq import parse_faq
from gsme_parser.markdown import write_faq_markdown
from gsme_parser.schedule import parse_schedule_csv, write_schedule_csv


def _print_warnings(warnings) -> None:
    for warning in warnings:
        location = f" ({warning.location})" if warning.location else ""
        print(f"warning [{warning.code}]{location}: {warning.message}", file=sys.stderr)


def _parse_faq_command(args: argparse.Namespace) -> int:
    result = parse_faq(args.input, strategy=args.strategy)
    _print_warnings(result.warnings)
    if not result.faq_records:
        print("No FAQ records were parsed", file=sys.stderr)
        return 1
    write_faq_markdown(result.faq_records, args.output, title=args.title)
    print(f"Parsed {len(result.faq_records)} FAQs to {args.output}")
    return 0


def _parse_schedule_command(args: argparse.Namespace) -> int:
    result = parse_schedule_csv(args.input, schedule_type=args.schedule_type)
    _print_warnings(result.warnings)
    if not result.schedule_records:
        print("No schedule records were parsed", file=sys.stderr)
        return 1
    write_schedule_csv(result.schedule_records, args.output)
    print(f"Parsed {len(result.schedule_records)} schedule rows to {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsme-parser",
        description="Parse FAQ documents and schedule CSVs for hybrid RAG",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    faq = commands.add_parser("faq", help="Parse a DOCX or born-digital PDF FAQ")
    faq.add_argument("input", type=Path)
    faq.add_argument("--output", "-o", required=True, type=Path)
    faq.add_argument(
        "--strategy",
        choices=["auto", "table", "paragraphs", "qa"],
        default="auto",
        help="Auto-detect by default; table/paragraphs apply to DOCX and table/qa to PDF",
    )
    faq.add_argument(
        "--title",
        default="Muse Semiconductor Frequently Asked Questions",
    )
    faq.set_defaults(handler=_parse_faq_command)

    schedule = commands.add_parser("schedule", help="Parse a shared or full schedule CSV")
    schedule.add_argument("input", type=Path)
    schedule.add_argument("--output", "-o", required=True, type=Path)
    schedule.add_argument("--schedule-type", choices=["shared", "full"])
    schedule.set_defaults(handler=_parse_schedule_command)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        status = args.handler(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
        return
    raise SystemExit(status)


if __name__ == "__main__":
    main()

