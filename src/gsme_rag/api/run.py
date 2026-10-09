import uvicorn
import os


def main() -> None:
    uvicorn.run(
        "gsme_rag.api.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
