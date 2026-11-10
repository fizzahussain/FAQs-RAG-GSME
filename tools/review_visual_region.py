"""Local source-region rendering/OCR. Optional Gemini drafts need explicit consent."""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from gsme_rag.parsing.visual_review import extract_region, describe_with_gemini


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--bbox", type=float, nargs=4)
    parser.add_argument("--gemini-draft", action="store_true")
    parser.add_argument("--confirm-external", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("validation/visual-review"))
    args = parser.parse_args()
    load_dotenv(override=False)
    if args.gemini_draft and not args.confirm_external:
        parser.error("--confirm-external is required to send the rendered region to Google")
    record, image = extract_region(args.source, args.page, args.bbox)
    if args.gemini_draft:
        record["vision"] = describe_with_gemini(image, os.getenv("GSME_GEMINI_MODEL", ""))
    args.output.mkdir(parents=True, exist_ok=True)
    stem = args.output / record["evidence_id"]
    stem.with_suffix(".png").write_bytes(image)
    stem.with_suffix(".json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"evidence_id": record["evidence_id"], "page": record["page"],
                      "review_required": True, "embedding_eligible": False, "output": str(stem.with_suffix(".json"))}))


if __name__ == "__main__":
    main()
