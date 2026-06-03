"""
Knowledge Graph Web API
Run: uvicorn api.main:app --reload --port 9000
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routers import edges, flow_proposals, graph, nodes, tracking, upload

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="Knowledge Graph API",
    description="Live CRUD for versioned Neo4j knowledge graph",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(graph.router)
app.include_router(nodes.router)
app.include_router(flow_proposals.router)
app.include_router(edges.router)
app.include_router(upload.router)
app.include_router(tracking.router)

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
