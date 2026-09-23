"""Start the AutoPoster browser interface."""

from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "web.app:app",
        host=os.getenv("AUTOPOSTER_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("AUTOPOSTER_WEB_PORT", "8000")),
        reload=False,
    )
