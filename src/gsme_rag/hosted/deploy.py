"""Managed-service entry point sharing one artifact store with an optional admin."""

import os

from gsme_rag.storage.config import from_env
from gsme_rag.storage.postgres import EvidenceStore
from .app import HostedSettings, create_hosted_app


def create_app():
    settings = from_env()
    hosted = HostedSettings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore(settings.database_url)
    store.migrate()
    app = create_hosted_app(settings, hosted, store=store)
    if os.getenv("GSME_HOSTED_ADMIN_ENABLED", "no") == "yes":
        from gsme_rag.storage.admin import create_admin_app
        app.mount("/operator", create_admin_app(settings, store, remote_origin=hosted.origin))
    return app


def main():
    import uvicorn

    uvicorn.run("gsme_rag.hosted.deploy:create_app", factory=True, host="0.0.0.0",
                port=int(os.getenv("PORT", "10000")), workers=1, proxy_headers=True,
                forwarded_allow_ips=os.getenv("GSME_TRUSTED_PROXY_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
