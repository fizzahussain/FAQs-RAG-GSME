from collections import defaultdict
from pathlib import Path

from gsme_parser.models import FAQRecord


def _escape_label_text(text: str) -> str:
    return text.replace("\r", "").strip()


def render_faq_markdown(
    records: list[FAQRecord],
    title: str = "Muse Semiconductor Frequently Asked Questions",
) -> str:
    grouped: dict[str, list[FAQRecord]] = defaultdict(list)
    for record in records:
        grouped[record.category].append(record)

    output = [f"# {title}"]
    for category, category_records in grouped.items():
        output.extend(["", f"## {category}"])
        for record in category_records:
            output.extend(
                [
                    "",
                    f"### FAQ {record.faq_id}",
                    "",
                    f"**Question:** {_escape_label_text(record.question)}",
                    "",
                    f"**Answer:** {_escape_label_text(record.answer)}",
                    "",
                    "---",
                ]
            )
    return "\n".join(output).rstrip() + "\n"


def write_faq_markdown(
    records: list[FAQRecord],
    output_path: str | Path,
    title: str = "Muse Semiconductor Frequently Asked Questions",
) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_faq_markdown(records, title=title),
        encoding="utf-8",
    )
