"""`python -m kg_pipeline.control`: serve the control API on port 8090."""
import os

import uvicorn

from .app import create_app


def main() -> None:
    app = create_app()  # exits with a clear error when KG_SECRET_KEY is missing
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))


if __name__ == "__main__":
    main()
