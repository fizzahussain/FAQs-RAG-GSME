from dataclasses import dataclass
import hashlib
import re

from gsme_rag.models import ChunkResult, DocumentMetadata, MarkdownChunk, ParseWarning
from gsme_rag.parsing.schedules import is_schedule_header, resolve_schedule_kind


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
    category: str | None = None


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


def _is_bold_paragraph(text: str) -> bool:
    value = text.strip()
    return (
        (value.startswith("**") and value.endswith("**"))
        or (value.startswith("__") and value.endswith("__"))
    )


def _merge_wrapped_questions(blocks: list[_Block]) -> list[_Block]:
    """Join numbered PDF question lines when the question mark wraps."""
    merged: list[_Block] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if (
            block.kind == "paragraph"
            and _is_bold_paragraph(block.text)
            and NUMBERED_QUESTION.match(_plain_text(block.text))
            and not _plain_text(block.text).endswith("?")
        ):
            parts = [_plain_text(block.text)]
            next_index = index + 1
            while next_index < len(blocks):
                following = blocks[next_index]
                if (
                    following.kind != "paragraph"
                    or not _is_bold_paragraph(following.text)
                    or NUMBERED_QUESTION.match(_plain_text(following.text))
                ):
                    break
                parts.append(_plain_text(following.text))
                next_index += 1
                if parts[-1].endswith("?"):
                    break
            if len(parts) > 1 and parts[-1].endswith("?"):
                merged.append(_Block(
                    kind="paragraph",
                    text=f"**{' '.join(parts)}**",
                    page_start=block.page_start,
                    page_end=blocks[next_index - 1].page_end,
                ))
                index = next_index
                continue
        merged.append(block)
        index += 1
    return merged


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
    category_column = next(
        (index for index, value in enumerate(header_cells) if value.strip().casefold() == "category"),
        None,
    )
    question_column = next(
        (index for index, value in enumerate(header_cells) if value.strip().casefold() == "question"),
        None,
    )
    answer_column = next(
        (index for index, value in enumerate(header_cells) if value.strip().casefold() == "answer"),
        None,
    )
    units = []
    for row_number, row in enumerate(lines[2:], start=block.first_data_row):
        cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
        cells = [re.sub(r"<br\s*/?>", "\n", cell.strip(), flags=re.I).replace("\\|", "|") for cell in cells]
        page = block.page_start
        page_end = block.page_end
        if page_column is not None and page_column < len(cells):
            value = cells[page_column].strip()
            page_range = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", value)
            if page_range:
                page = int(page_range.group(1))
                page_end = int(page_range.group(2) or page_range.group(1))
        category = cells[category_column] if category_column is not None and category_column < len(cells) else None
        is_faq = (
            question_column is not None and answer_column is not None
            and question_column < len(cells) and answer_column < len(cells)
            and bool(cells[question_column])
        )
        anchor = f"Question: {cells[question_column]}\n\nAnswer:" if is_faq else ""
        body = cells[answer_column] if is_faq else f"{header}\n{row}"
        units.append(_Unit(
            kind="table_row",
            body=body,
            heading_path=[category] if category else heading_path.copy(),
            page_start=page,
            page_end=page_end,
            anchor=anchor,
            table_row_number=row_number,
            category=category,
        ))
    return units


def _blocks_to_units(blocks: list[_Block], split_paragraphs: bool = False) -> list[_Unit]:
    units: list[_Unit] = []
    heading_stack: list[tuple[int, str]] = []
    generic: list[_Block] = []

    def current_path() -> list[str]:
        return [title for _, title in heading_stack]

    def flush_generic() -> None:
        nonlocal generic
        if not generic:
            return
        units.append(
            _Unit(
                kind="section",
                body="\n\n".join(block.text for block in generic),
                heading_path=current_path(),
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
            heading_stack = [(depth, title) for depth, title in heading_stack if depth < level]
            heading_stack.append((level, block.title or block.text))
            index += 1
            continue

        if block.kind == "table":
            flush_generic()
            units.extend(_table_units(block, current_path()))
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
                    heading_path=current_path(),
                    page_start=block.page_start,
                    page_end=(answer_blocks[-1].page_end if answer_blocks else block.page_end),
                )
            )
            index = next_index
            continue

        if generic and (
            block.page_start != generic[-1].page_end
            or (split_paragraphs and generic[-1].text.rstrip().endswith((".", "?", "!", "…")))
        ):
            flush_generic()
        generic.append(block)
        index += 1

    flush_generic()
    return units


def chunk_markdown(
    markdown: str,
    source_file: str = "normalized.md",
    max_chars: int = 1800,
    strategy: str = "auto",
    overlap_chars: int = 120,
    metadata: DocumentMetadata | None = None,
    semantic_schedule_rows: bool = False,
) -> ChunkResult:
    if not 200 <= max_chars <= 20_000:
        raise ValueError("max_chars must be between 200 and 20000")
    if strategy not in {"auto", "fixed_size", "layout_aware", "hybrid", "paragraph_aware"}:
        raise ValueError("strategy must be auto, fixed_size, layout_aware, hybrid, or paragraph_aware")
    if strategy == "hybrid" and not 0 <= overlap_chars <= max_chars // 4:
        raise ValueError("overlap_chars must be between 0 and one quarter of max_chars")
    if not markdown.strip():
        return ChunkResult(
            warnings=[
                ParseWarning(code="empty_markdown", message="Markdown input is empty")
            ]
        )

    normalized_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    if metadata and metadata.source_file != source_file:
        raise ValueError("source_file must match the parse metadata source_file")
    blocks = _merge_wrapped_questions(_parse_blocks(markdown))
    schedule_tables = [
        block for block in blocks if block.kind == "table"
        and is_schedule_header(block.text.splitlines()[0].strip("|").split("|"))
    ]
    is_schedule = bool(schedule_tables) or bool(metadata and metadata.document_class == "schedule")
    kind = metadata.schedule_kind if metadata else None
    if is_schedule and not kind:
        kind = resolve_schedule_kind(source_file)
    if is_schedule and kind:
        resolve_schedule_kind(source_file, kind)  # reject conflicting filename metadata
    if is_schedule and not kind:
        raise ValueError("Schedule type is unknown. Select Full or Shared before chunking or ingesting.")
    if metadata and metadata.classification_status != "ready" and is_schedule:
        # A newer explicit selection must come through a new parse request.
        raise ValueError("Schedule metadata needs review. Reparse with Full or Shared selected.")
    if is_schedule and not semantic_schedule_rows:
        return ChunkResult(warnings=[ParseWarning(
            code="structured_records_only",
            message="Schedule rows are typed records for exact filtering. Enable semantic schedule rows to create optional discovery chunks.",
        )])
    if is_schedule and strategy == "fixed_size":
        raise ValueError("Fixed-size chunking is a comparison baseline; schedule discovery requires row-preserving chunking.")

    if strategy == "fixed_size":
        units = [
            _Unit("fixed_size", part, [], None, None)
            for part in _split_text(markdown.strip(), max_chars)
        ]
    else:
        units = _blocks_to_units(
            blocks, split_paragraphs=strategy in {"auto", "paragraph_aware"}
        )
    chunks: list[MarkdownChunk] = []
    warnings: list[ParseWarning] = []
    if metadata and normalized_hash != metadata.normalized_sha256:
        warnings.append(ParseWarning(
            code="markdown_edited",
            message="Markdown changed after parsing; the chunk hash identifies this edited version.",
        ))
        if is_schedule and semantic_schedule_rows:
            warnings.append(ParseWarning(
                code="source_row_mapping_invalid",
                message="Edited schedule Markdown cannot be linked reliably to CSV source rows",
            ))

    for unit_index, unit in enumerate(units):
        if unit.kind in {"question_answer", "table_row"} and unit.anchor and not unit.body.strip():
            warnings.append(ParseWarning(
                code="faq_answer_missing",
                message="A question has no answer in the normalized source; no evidence chunk was created",
                location=f"table row {unit.table_row_number}" if unit.table_row_number else None,
            ))
            continue
        context = _heading_markdown(unit.heading_path)
        anchor = unit.anchor.strip()
        fixed_parts = [part for part in (context, anchor) if part]
        fixed = "\n\n".join(fixed_parts)
        body_limit = max(80, max_chars - len(fixed) - (2 if fixed else 0))
        if unit.kind in {"table", "table_row"} and not unit.anchor:
            # A row is one record; cutting it can detach values from column names.
            body_parts = [unit.body]
        elif unit.body:
            use_overlap = strategy in {"auto", "hybrid"} and unit.kind == "section" and len(unit.body) > body_limit
            effective_overlap = min(overlap_chars, body_limit // 4) if use_overlap else 0
            part_limit = body_limit - effective_overlap
            body_parts = _split_text(unit.body, part_limit)
            if use_overlap:
                body_parts = _overlap_parts(body_parts, body_limit, effective_overlap)
        else:
            body_parts = [""]

        source_id = metadata.source_id if metadata else f"md-{normalized_hash[:16]}"
        unit_digest = hashlib.sha256(
            f"{source_id}|{normalized_hash}|{unit_index}|{unit.kind}|{unit.body}".encode("utf-8")
        ).hexdigest()[:16]
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
            digest = hashlib.sha256(
                f"{source_id}|{normalized_hash}|{strategy}|{index}|{text}".encode("utf-8")
            ).hexdigest()[:16]
            chunks.append(
                MarkdownChunk(
                    chunk_id=f"chunk-{digest}",
                    index=index,
                    text=text,
                    chunk_type=unit.kind,
                    source_file=source_file,
                    heading_path=unit.heading_path.copy(),
                    category=unit.category or (
                        unit.heading_path[-1]
                        if unit.kind == "question_answer" and unit.heading_path else None
                    ),
                    source_page_start=unit.page_start,
                    source_page_end=unit.page_end,
                    table_row_number=unit.table_row_number,
                    unit_id=f"unit-{unit_digest}",
                    part_index=part_index,
                    part_count=len(body_parts),
                    source_id=source_id,
                    schedule_kind=kind,
                    source_row_number=(
                        metadata.csv_source_rows[unit.table_row_number - 2]
                        if is_schedule and metadata and metadata.csv_source_rows
                        and normalized_hash == metadata.normalized_sha256
                        and unit.table_row_number is not None
                        and 2 <= unit.table_row_number < len(metadata.csv_source_rows) + 2
                        else None
                    ),
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
