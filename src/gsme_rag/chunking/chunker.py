from dataclasses import dataclass
import hashlib
import re

from gsme_rag.models import ChunkResult, MarkdownChunk, ParseWarning


PAGE_MARKER = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->$")
TABLE_ROW_MARKER = re.compile(r"^<!--\s*table:\s*\d+;\s*first_data_row:\s*(\d+)\s*-->$")
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
    first_data_row: int = 2


@dataclass(slots=True)
class _Unit:
    kind: str
    body: str
    heading_path: list[str]
    page_start: int | None
    page_end: int | None
    anchor: str = ""
    table_row_number: int | None = None


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
    first_data_row = 2
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

        table_marker = TABLE_ROW_MARKER.match(stripped)
        if table_marker:
            first_data_row = int(table_marker.group(1))
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
                    first_data_row=first_data_row,
                )
            )
            first_data_row = 2
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


def _split_long_piece(text: str, limit: int, by_sentence: bool = True) -> list[str]:
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text) if by_sentence else []
    if len(sentences) > 1:
        pieces = sentences
    else:
        pieces = text.split()
    output: list[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > limit:
            if current:
                output.append(current)
                current = ""
            if by_sentence and len(piece.split()) > 1:
                output.extend(_split_long_piece(piece, limit, by_sentence=False))
            else:
                output.extend(piece[start : start + limit] for start in range(0, len(piece), limit))
            continue
        candidate = f"{current} {piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            output.append(current)
            current = piece
    if current:
        output.append(current)
    return output


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    pieces = [piece.strip() for piece in re.split(r"\n\s*\n", text) if piece.strip()]
    output: list[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > limit:
            if current:
                output.append(current)
                current = ""
            output.extend(_split_long_piece(piece, limit))
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


def _overlap_parts(parts: list[str], limit: int, overlap_chars: int) -> list[str]:
    if len(parts) < 2 or overlap_chars == 0:
        return parts
    result = [parts[0]]
    for previous, part in zip(parts, parts[1:]):
        sentences = re.split(r"(?<=[.!?])\s+", previous.strip())
        tail: list[str] = []
        for sentence in reversed(sentences):
            if not re.search(r"[.!?]$", sentence.strip().rstrip("*_")):
                break
            candidate = " ".join(reversed([*tail, sentence]))
            if candidate.count("*") % 2:
                break
            if len(candidate) > overlap_chars:
                break
            tail.append(sentence)
        prefix = " ".join(reversed(tail))
        result.append(f"{prefix}\n\n{part}" if prefix else part)
    return result


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
    header_cells = re.split(r"(?<!\\)\|", lines[0].strip().strip("|"))
    page_column = next(
        (index for index, value in enumerate(header_cells) if value.strip().casefold() == "page"),
        None,
    )
    units = []
    for row_number, row in enumerate(lines[2:], start=block.first_data_row):
        cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
        page = block.page_start
        if page_column is not None and page_column < len(cells):
            value = cells[page_column].strip()
            if value.isdecimal():
                page = int(value)
        units.append(_Unit(
            kind="table_row",
            body=f"{header}\n{row}",
            heading_path=heading_path.copy(),
            page_start=page,
            page_end=page,
            table_row_number=row_number,
        ))
    return units


def _blocks_to_units(blocks: list[_Block], split_paragraphs: bool = False) -> list[_Unit]:
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

        if generic and (split_paragraphs or block.page_start != generic[-1].page_end):
            flush_generic()
        generic.append(block)
        index += 1

    flush_generic()
    return units


def chunk_markdown(
    markdown: str,
    source_file: str = "normalized.md",
    max_chars: int = 1800,
    strategy: str = "layout_aware",
    overlap_chars: int = 120,
) -> ChunkResult:
    if not 200 <= max_chars <= 20_000:
        raise ValueError("max_chars must be between 200 and 20000")
    if strategy not in {"fixed_size", "layout_aware", "hybrid", "paragraph_aware"}:
        raise ValueError("strategy must be fixed_size, layout_aware, hybrid, or paragraph_aware")
    if strategy == "hybrid" and not 0 <= overlap_chars <= max_chars // 4:
        raise ValueError("overlap_chars must be between 0 and one quarter of max_chars")
    if not markdown.strip():
        return ChunkResult(
            warnings=[
                ParseWarning(code="empty_markdown", message="Markdown input is empty")
            ]
        )

    if strategy == "fixed_size":
        units = [
            _Unit("fixed_size", part, [], None, None)
            for part in _split_text(markdown.strip(), max_chars)
        ]
    else:
        units = _blocks_to_units(
            _parse_blocks(markdown), split_paragraphs=strategy == "paragraph_aware"
        )
    chunks: list[MarkdownChunk] = []
    warnings: list[ParseWarning] = []

    for unit_index, unit in enumerate(units):
        context = _heading_markdown(unit.heading_path)
        anchor = unit.anchor.strip()
        fixed_parts = [part for part in (context, anchor) if part]
        fixed = "\n\n".join(fixed_parts)
        body_limit = max(80, max_chars - len(fixed) - (2 if fixed else 0))
        if unit.kind in {"table", "table_row"}:
            # A row is one record; cutting it can detach values from column names.
            body_parts = [unit.body]
        elif unit.body:
            use_overlap = strategy == "hybrid" and unit.kind == "section"
            effective_overlap = min(overlap_chars, body_limit // 4) if use_overlap else 0
            part_limit = body_limit - effective_overlap
            body_parts = _split_text(unit.body, part_limit)
            if use_overlap:
                body_parts = _overlap_parts(body_parts, body_limit, effective_overlap)
        else:
            body_parts = [""]

        unit_digest = hashlib.sha1(
            f"{source_file}|{unit_index}|{unit.kind}|{unit.body}".encode("utf-8")
        ).hexdigest()[:12]
        for part_index, body in enumerate(body_parts):
            text = "\n\n".join(part for part in (fixed, body) if part).strip()
            if not text:
                continue
            if len(text) > max_chars:
                warnings.append(
                    ParseWarning(
                        code="oversized_unit",
                        message=(
                            "This table row or repeated context exceeds max_chars; "
                            "it was kept intact for inspection"
                        ),
                        location=f"chunk {len(chunks)}",
                    )
                )
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
                    table_row_number=unit.table_row_number,
                    unit_id=f"unit-{unit_digest}",
                    part_index=part_index,
                    part_count=len(body_parts),
                )
            )

    if not chunks:
        warnings.append(
            ParseWarning(
                code="no_chunks",
                message="No chunkable Markdown content was found",
            )
        )
    return ChunkResult(chunks=chunks, warnings=warnings)
