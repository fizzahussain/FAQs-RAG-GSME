"""Local operator publication of one verified FAQ or schedule source version.

Never guess visibility from a filename. Private uploads cannot invoke this tool
through the hosted API; the operator must inspect the original before publishing.
"""

from argparse import ArgumentParser
import getpass
import hmac
import os
from uuid import UUID

from gsme_rag.hosted.access import publish_faq
from gsme_rag.storage.config import from_env
from gsme_rag.storage.postgres import EvidenceStore


def main():
    cli = ArgumentParser(description=__doc__)
    cli.add_argument("--tenant", required=True)
    cli.add_argument("--document-id", type=UUID, required=True)
    cli.add_argument("--version-id", type=UUID, required=True)
    cli.add_argument("--kind", choices=("faq", "schedule"), required=True)
    cli.add_argument("--confirm-public", action="store_true")
    args = cli.parse_args()
    settings = from_env()
    if not args.confirm_public:
        cli.error("Inspect the exact original and use --confirm-public to authorize guest visibility")
    secret = os.getenv("GSME_REVIEW_AUTH_TOKEN", "")
    if len(secret) < 20 or not hmac.compare_digest(getpass.getpass("Local operator token: ").encode(), secret.encode()):
        raise SystemExit("Operator authorization denied")
    publish_faq(EvidenceStore(settings.database_url), args.tenant, str(args.document_id), str(args.version_id), schedule=args.kind == "schedule")
    print("Published this exact version: " + ("schedule records only; original download remains private" if args.kind == "schedule" else "public FAQ and original"))


if __name__ == "__main__":
    main()
