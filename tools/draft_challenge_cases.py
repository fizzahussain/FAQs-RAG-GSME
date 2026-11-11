"""Draft source-pinned positive and near-topic negative cases, local-only.

These are NOT human-reviewed labels. Negative claims about missing information
need a full scoped-corpus check, not merely absence from the nearest passage.
Output is restricted to ignored validation/ to keep company material out of Git.
"""

from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import re

from gsme_rag.storage.config import from_env
from gsme_rag.storage.files import checksum
from gsme_rag.storage.postgres import EvidenceStore


# Original topic fragment, answerable paraphrase, near-topic unsupported request.
PAIRS = [
    ("legal agreements", "Which agreements must I sign before working with Muse?", "What exact monetary penalty does Muse charge for breaching its NDA?"),
    ("get a quote", "How can I request a quotation for my project?", "What is the fixed USD price for my 28nm HPC+ RF shared tapeout?"),
    ("reschedule or cancel", "What should I do if my tapeout must be postponed?", "What exact refund percentage will I receive if I cancel 48 hours before tapeout?"),
    ("commercial companies", "Can a commercial business use Muse services?", "Which named commercial customers used Muse last quarter and how much did each pay?"),
    ("acknowledge muse", "How should I acknowledge Muse in a research publication?", "What is the DOI of my unpublished paper acknowledging Muse?"),
    ("gds upload deadlines", "What are the preliminary and final GDS submission requirements?", "What password should I use to access the private GDS upload account?"),
    ("mpw reservation", "How do I reserve an MPW slot?", "Has my Project ZX999 MPW reservation already been approved?"),
    ("minimum die area", "What is the smallest die area I can reserve?", "What exact dimensions did my Project ZX999 team purchase?"),
    ("additional samples", "Can I request more samples than the standard allocation?", "How many extra samples from Project ZX999 are physically in inventory today?"),
    ("access a pdk", "Can Muse help me obtain a process design kit?", "What is the exact download credential for my private PDK account?"),
    ("installing the pdk", "Who can help me with PDK installation questions?", "What exact shell command fixes PDK error ZX999 on my workstation?"),
    ("wafer thickness", "Can I select the thickness of MPW samples?", "What thickness was actually measured on my Project ZX999 wafer yesterday?"),
    ("samples bumped", "Does Muse offer bumping for samples?", "Which bump failed inspection on my Project ZX999 lot and what was its measured diameter?"),
    ("corner/skew wafers", "Can I request corner or skew wafers?", "What are the measured corner results for my Project ZX999 lot?"),
    ("what is a shared block", "How does the Shared Block tapeout service work?", "What are the names and designs of the other customers sharing my block?"),
    ("coordinate with other customers", "Do I need to coordinate directly with other Shared Block customers?", "What private email addresses belong to every customer sharing my tapeout?"),
]


def main():
    cli = ArgumentParser(description=__doc__)
    cli.add_argument("--tenant", default="local-test")
    cli.add_argument("--output", type=Path, default=Path("validation/challenge-benchmark"))
    args = cli.parse_args()
    if not args.output.resolve().is_relative_to((Path(__file__).resolve().parents[1] / "validation").resolve()):
        raise ValueError("Private drafts must stay under ignored validation/")
    store = EvidenceStore(from_env().database_url)
    with store.connect() as db:
        rows = db.execute("""SELECT c.source_file,c.version_id,c.source_sha256,c.source_chunk_id,
            c.faq_question,c.evidence_text,v.original_path FROM approved_current_evidence c
            JOIN document_versions v ON v.id=c.version_id WHERE c.tenant_id=%s
            AND c.source_file IN ('FAQs QA Format.docx','MFG-GSME-0002.pdf')
            ORDER BY c.source_file,c.chunk_index""", (args.tenant,)).fetchall()
    sources = {}
    for row in rows:
        if row["source_file"] not in sources:
            if checksum(Path(row["original_path"])) != row["source_sha256"]:
                raise ValueError("Original source changed; do not draft cases from stale evidence")
            sources[row["source_file"]] = dict(version_id=str(row["version_id"]), source_sha256=row["source_sha256"])
    cases = []

    def add(question, kind, row=None, pair=None, rationale=None):
        identity = sha256((kind + "|" + question).encode()).hexdigest()[:16]
        cases.append({"id": "case-" + identity, "tenant_id": args.tenant, "question": question,
            "kind": kind, "pair_id": pair, "label_status": "draft_needs_human_source_review",
            "group_id": pair or identity, "split": "unassigned", "rationale": rationale,
            "expected_evidence": ([dict(version_id=str(row["version_id"]), chunk_id=row["source_chunk_id"],
                source_sha256=row["source_sha256"])] if row else []),
            "expect_no_answer": kind == "near_topic_negative"})

    faq = [row for row in rows if row["faq_question"]]
    for row in faq:
        add(re.sub(r"^\s*\d+\s+", "", row["faq_question"]), "original_faq", row,
            pair=row["source_chunk_id"], rationale="Source wording, not a held-out paraphrase")
    for topic, positive, negative in PAIRS:
        row = next((r for r in faq if topic in r["faq_question"].casefold()), None)
        if row:
            add(positive, "paraphrase", row, pair=row["source_chunk_id"])
            add(negative, "near_topic_negative", pair=row["source_chunk_id"],
                rationale="Related source may rank highly but does not establish the requested price, credential, customer identity or project-specific measurement")
    manual_pairs = [
        ("MTTF", "What does MTTF stand for?", "What MTTF was measured for Project ZX999 during its latest reliability run?"),
        ("TSV", "What does TSV stand for in semiconductor manufacturing?", "What TSV resistance was measured on Project ZX999 wafer 17 yesterday?"),
        ("uHAST", "What is the purpose of the uHAST reliability test?", "Did Project ZX999 pass uHAST testing last week?"),
    ]
    for term, positive, negative in manual_pairs:
        row = next((r for r in rows if r["source_file"] == "MFG-GSME-0002.pdf"
                    and re.search(r"\b" + re.escape(term) + r"\b", r["evidence_text"], re.I)), None)
        if row:
            # A term hit is only a candidate annotation. Human review must choose
            # the actual definition passage and any additional required support.
            add(positive, "manual_definition_draft", row, pair="manual-" + term,
                rationale="Term-matched candidate; verify this passage actually defines the term")
            add(negative, "near_topic_negative", pair="manual-" + term,
                rationale="Generic process definitions do not establish private project results")
    if any("ZX999" in r["evidence_text"] for r in rows):
        raise ValueError("Synthetic project identifier already exists; replace negative examples")
    for group, question, reason in [
        ("quality-formula", "Transcribe every fraction, denominator and symbol position in the control-chart formulas on page 163 exactly.",
         "Known formula-fidelity gaps are not resolved by generic nearby text; verify whether a reviewed transcription exists"),
        ("quality-chart", "Which category belongs to each of the six chart percentages on page 101?",
         "Extracted labels do not verify visual category/value associations"),
        ("quality-link", "What exact website address does the missing 'here' link for the Shared Block schedule point to?",
         "A missing source hyperlink cannot be reconstructed from general schedule guidance")]:
        add(question, "near_topic_negative", pair=group, rationale=reason)
    probes = []
    for row in rows:
        if row["source_file"] != "MFG-GSME-0002.pdf":
            continue
        body = " ".join(line.strip() for line in row["evidence_text"].splitlines()
                        if line.strip() and not line.lstrip().startswith("#"))
        if not body:
            continue
        excerpt = " ".join(body.split()[:20])
        probes.append({"id": "probe-" + row["source_chunk_id"], "kind": "lexical_coverage_probe_not_acceptance_case",
            "question": f'Which information does the document provide about this passage: "{excerpt}"?',
            "expected_evidence": [dict(version_id=str(row["version_id"]), chunk_id=row["source_chunk_id"], source_sha256=row["source_sha256"])],
            "label_status": "draft_needs_original_source_review",
            "note": "Contains copied source wording; useful for plumbing coverage, not held-out semantic accuracy."})
    report = {"source": "draft_not_human_reviewed", "sources": sources, "cases": cases, "coverage_probes": probes,
        "external_calls": 0, "instructions": "Verify positives against originals and negatives against the entire allowed corpus. Keep paired source groups in one split; do not tune on held-out questions. Related retrieval is allowed for negatives; the final answer must abstain or clarify."}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "cases.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Draft challenge questions", "", "Not human-reviewed. No model evaluation has been run.", "",
             "| Type | Question | Source / expected behavior |", "| --- | --- | --- |"]
    for row in cases:
        source = row["expected_evidence"][0]["chunk_id"] if row["expected_evidence"] else "Abstain: requested fact is not established; verify whole corpus"
        lines.append(f"| {row['kind']} | {row['question'].replace('|', '/')} | {source} |")
    (args.output / "QUESTIONS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    coverage = ["# Per-chunk coverage probes", "", "Copied-wording smoke probes, not a semantic-accuracy benchmark.", "",
                "| Chunk | Question |", "| --- | --- |"]
    coverage.extend(f"| {r['expected_evidence'][0]['chunk_id']} | {r['question'].replace('|', '/')} |" for r in probes)
    (args.output / "COVERAGE_PROBES.md").write_text("\n".join(coverage) + "\n", encoding="utf-8")
    print(json.dumps({"questions": len(cases), "negative_drafts": sum(c["expect_no_answer"] for c in cases),
                      "manual_chunk_coverage_probes": len(probes), "source_files": list(sources), "external_calls": 0, "output": str(args.output)}))


if __name__ == "__main__":
    main()
