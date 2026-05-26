#!/usr/bin/env python3
"""Start the Knowledge Graph API + frontend."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=9000, reload=True)
