# 0.16.1 — source links and a focused console

## Links in evidence

Source hyperlinks belong in the same chunk as their question and answer:

```markdown
Question: Where is the schedule?
Answer: The schedule can be found [here](<https://example.invalid/shared-schedule>).
```

The address above is a synthetic illustration, not a GSME schedule URL. The linked website is **not** downloaded, parsed, or verified simply because its URL appears in a chunk. Import the actual schedule CSV/PDF separately to search its contents.

DOCX extraction now retains hyperlink labels and destinations, including table cells. Native and Docling PDF results pass through a shared annotation-recovery step. A uniquely aligned source line gets its inline link; ambiguous placement retains a reviewed source-page/bounding-box note rather than assigning a repeated “here” to the wrong question. Only supported HTTP(S)/mailto links become clickable. Source HTML is rendered as text, never executed.

“Found here”, “available here” and “click here” without a usable link produce `link_target_missing`. Those chunks remain review-required, and the warning reaches review cards even for unpaged Word content. URLs are not guessed or replaced from another document. Literal fenced code is not treated as a missing-link claim.

Source check: `FAQs Table Format.pdf` contains four pages and zero link annotations. Its companion `FAQs Table Format.docx` also has zero hyperlinks. The page-4 Shared Block schedule answer therefore still says “can be found here”; no destination can be recovered from these sources. A native rerun retains all 31 FAQ rows, source-page attribution and review flags, and reports the missing link on page 4. The content route remains `faq`. Obtain a source containing the original hyperlink, or add the verified target to a corrected source and ingest a new version.

## Simpler interface

- Four keyboard-accessible screens: **Documents**, **Review & index**, **Ask sources**, **Schedules**. Only one screen is visible at a time.
- Connection settings collapse after connection. Working mode is shared across screens; Cohere consent controls appear only in real-provider mode.
- **Upload & parse** combines the normal file-preparation actions, without approving evidence. Existing document versions remain selectable.
- Review shows ten chunks per page with collapsible cards and readable source warnings. Originals, Markdown, diagnostics, job IDs and index internals remain available under details.
- **Make searchable** publishes/indexes already-approved chunks in up to twenty bounded batches per click. Failed/future-due work remains visible; approval and external-content checks are unchanged.
- Ask sources emphasizes the question, readable cited evidence and safe clickable links. Ranking comparisons, identifiers, hashes and raw responses are collapsed. Mandatory reranking is unchanged; this still does not generate an answer.
- Schedule CSV uploads lead to exact schedule search instead of semantic indexing. Changing workspace/token clears old document/results state and requires reconnection.
- Responsive layout wraps long source hashes and supports small screens. Versioned static-asset URLs prevent an old cached script from being paired with the new HTML.

Browser verification also exposed a cold-cache demo failure: FakeEmbeddings downloaded tokenizer data on first use despite the offline-demo label. It now uses a conservative UTF-8 byte counter entirely offline. Synthetic vectors/model identity are unchanged; real Cohere tokenization is unchanged. The unused application dependency on tiktoken was removed. This synthetic counter is not a model-token estimate.

## Verification and upgrade

Focused regressions cover DOCX/PDF links, the shared Docling-output hook, unsafe destinations, ambiguity, missing URL warnings/page attribution, unpaged review warnings and offline demo operation. The optional `tools/verify_console_ui.py` exercises real headless Edge against an isolated test database: upload, automatic parsing, single-chunk source approval, publish/index, mandatory-reranked search, clickable links, inert source HTML, keyboard tabs, workspace reset and desktop/mobile layouts. It blocks external browser requests and stores screenshots only under ignored `validation/console-ui/`. Playwright is a test-only installation, not an application dependency.

Verification on 5 November 2026: **212 tests passed, zero skips**, in 69.238 seconds with real isolated PostgreSQL/Qdrant and the configured manual checks. The final headless Edge workflow passed on desktop/mobile with no page errors, no external page requests and no horizontal mobile overflow. No live private-document reranking was performed for this increment.

Application/parser/chunker versions are 0.16.1; retrieval algorithm identity stays 0.16.0. Old chunks do not gain URLs automatically: ingest a new version and review it before publication. No SQL migration is added. Restart the console and refresh the browser after upgrading; private `.env`, originals, review decisions and indexes are preserved.
