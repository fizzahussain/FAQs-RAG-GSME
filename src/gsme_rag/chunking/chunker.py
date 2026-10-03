from dataclasses import dataclass
import hashlib
import re

from gsme_rag.models import ChunkResult, MarkdownChunk, ParseWarning


PAGE_MARKER = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->$")
HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
TABLE_DIVIDER = re.compile(r"^\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
NUMBERED_QUESTION = re.compile(r"^\d{1,3}[.)]?\s+")


@dataclass(slots=True)
class _Block:
    kind: str
    text: str
    page_start: int | None
    page_end: int | None
    level: int | None = None
    title: str | None = None


@dataclass(slots=True)
class _Unit:
    kind: str
    body: str
    heading_path: list[str]
    page_start: int | None
    page_end: int | None
    anchor: str = ""


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and lines[index].lstrip().startswith("|")
        and bool(TABLE_DIVIDER.match(lines[index + 1].strip()))
    )


def _parse_blocks(markdown: str) -> list[_Block]:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[_Block] = []
    current_page: int | None = None
    index = 0

    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue

        page_match = PAGE_MARKER.match(stripped)
        if page_match:
            current_page = int(page_match.group(1))
            index += 1
            continue

        if stripped.startswith("<!--") and stripped.endswith("-->"):
            index += 1
            continue

        heading_match = HEADING.match(stripped)
        if heading_match:
            blocks.append(
                _Block(
                    kind="heading",
                    text=stripped,
                    page_start=current_page,
                    page_end=current_page,
                    level=len(heading_match.group(1)),
                    title=heading_match.group(2).strip(),
                )
            )
            index += 1
            continue

        if _is_table_start(lines, index):
            table_lines = [lines[index].strip(), lines[index + 1].strip()]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            blocks.append(
                _Block(
                    kind="table",
                    text="\n".join(table_lines),
                    page_start=current_page,
                    page_end=current_page,
                )
            )
            continue

        paragraph_lines = [stripped]
        start_page = current_page
        end_page = current_page
        index += 1
        while index < len(lines):
            candidate = lines[index].strip()
            if not candidate:
                break
            page_match = PAGE_MARKER.match(candidate)
            if page_match or HEADING.match(candidate) or _is_table_start(lines, index):
                break
            if candidate.startswith("<!--") and candidate.endswith("-->"):
                break
            paragraph_lines.append(candidate)
            end_page = current_page
            index += 1
        blocks.append(
            _Block(
                kind="paragraph",
                text="\n".join(paragraph_lines),
                page_start=start_page,
                page_end=end_page,
            )
        )

    return blocks


def _plain_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^(?:\*\*|__)", "", cleaned)
    cleaned = re.sub(r"(?:\*\*|__)$", "", cleaned)
    return cleaned.strip()


def _is_question(text: str) -> bool:
    stripped = text.strip()
    plain = _plain_text(stripped)
    is_bold = (
        (stripped.startswith("**") and stripped.endswith("**"))
        or (stripped.startswith("__") and stripped.endswith("__"))
    )
    return plain.endswith("?") and (is_bold or bool(NUMBERED_QUESTION.match(plain)))


def _heading_markdown(path: list[str]) -> str:
    return "\n".join(f"{'#' * min(index + 1, 6)} {title}" for index, title in enumerate(path))


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    pieces = [piece.strip() for piece in re.split(r"\n\s*\n", text) if piece.strip()]
    if len(pieces) == 1:
        pieces = [
            piece.strip()
            for piece in re.split(r"(?<=[.!?])\s+", text)
            if piece.strip()
        ]

    output: list[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > limit:
            if current:
                output.append(current)
                current = ""
            output.extend(piece[start : start + limit] for start in range(0, len(piece), limit))
            continue
        candidate = f"{current}\n\n{piece}".strip() if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            output.append(current)
            current = piece
    if current:
        output.append(current)
    return output


def _table_units(block: _Block, heading_path: list[str]) -> list[_Unit]:
    lines = block.text.splitlines()
    if len(lines) <= 2:
        return [
            _Unit(
                kind="table",
                body=block.text,
                heading_path=heading_path.copy(),
                page_start=block.page_start,
                page_end=block.page_end,
            )
        ]

    header = "\n".join(lines[:2])
    return [
        _Unit(
            kind="table_row",
            body=f"{header}\n{row}",
            heading_path=heading_path.copy(),
            page_start=block.page_start,
            page_end=block.page_end,
        )
        for row in lines[2:]
    ]


def _blocks_to_units(blocks: list[_Block]) -> list[_Unit]:
    units: list[_Unit] = []
    heading_path: list[str] = []
    generic: list[_Block] = []

    def flush_generic() -> None:
        nonlocal generic
        if not generic:
            return
        units.append(
            _Unit(
                kind="section",
                body="\n\n".join(block.text for block in generic),
                heading_path=heading_path.copy(),
                page_start=generic[0].page_start,
                page_end=generic[-1].page_end,
            )
        )
        generic = []

    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.kind == "heading":
            flush_generic()
            level = block.level or 1
            heading_path = heading_path[: level - 1]
            heading_path.append(block.title or block.text)
            index += 1
            continue

        if block.kind == "table":
            flush_generic()
            units.extend(_table_units(block, heading_path))
            index += 1
            continue

        if _is_question(block.text):
            flush_generic()
            answer_blocks: list[_Block] = []
            next_index = index + 1
            while next_index < len(blocks):
                following = blocks[next_index]
                if following.kind in {"heading", "table"}:
                    break
                if _is_question(following.text):
                    break
                answer_blocks.append(following)
                next_index += 1
            units.append(
                _Unit(
                    kind="question_answer",
                    anchor=block.text,
                    body="\n\n".join(item.text for item in answer_blocks),
                    heading_path=heading_path.copy(),
                    page_start=block.page_start,
                    page_end=(answer_blocks[-1].page_end if answer_blocks else block.page_end),
                )
            )
            index = next_index
            continue

        generic.append(block)
        index += 1

    flush_generic()
    return units


def chunk_markdown(
    markdown: str,
    source_file: str = "normalized.md",
    max_chars: int = 1800,
) -> ChunkResult:
    if not 200 <= max_chars <= 20_000:
        raise ValueError("max_chars must be between 200 and 20000")
    if not markdown.strip():
        return ChunkResult(
            warnings=[
                ParseWarning(code="empty_markdown", message="Markdown input is empty")
            ]
        )

    blocks = _parse_blocks(markdown)
    units = _blocks_to_units(blocks)
    chunks: list[MarkdownChunk] = []

    for unit in units:
        context = _heading_markdown(unit.heading_path)
        anchor = unit.anchor.strip()
        fixed_parts = [part for part in (context, anchor) if part]
        fixed = "\n\n".join(fixed_parts)
        body_limit = max(80, max_chars - len(fixed) - (2 if fixed else 0))
        body_parts = _split_text(unit.body, body_limit) if unit.body else [""]

        for body in body_parts:
            text = "\n\n".join(part for part in (fixed, body) if part).strip()
            if not text:
                continue
            index = len(chunks)
            digest = hashlib.sha1(
                f"{source_file}|{index}|{text}".encode("utf-8")
            ).hexdigest()[:12]
            chunks.append(
                MarkdownChunk(
                    chunk_id=f"chunk-{digest}",
                    index=index,
                    text=text,
                    chunk_type=unit.kind,
                    source_file=source_file,
                    heading_path=unit.heading_path.copy(),
                    source_page_start=unit.page_start,
                    source_page_end=unit.page_end,
                )
            )

    warnings: list[ParseWarning] = []
    if not chunks:
        warnings.append(
            ParseWarning(
                code="no_chunks",
                message="No chunkable Markdown content was found",
            )
        )
    return ChunkResult(chunks=chunks, warnings=warnings)
