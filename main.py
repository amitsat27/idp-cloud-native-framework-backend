from fastapi import FastAPI
from app.core.config import settings
from app.api.routes import intent, cluster, plan  # <-- Import the new cluster route
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title=settings.PROJECT_NAME)

# Register the routes with clean prefixes and tags for the Swagger UI
app.include_router(intent.router, prefix="/api/v1/intent", tags=["Orchestration"])
app.include_router(cluster.router, prefix="/api/v1/cluster", tags=["Cluster"])
app.include_router(plan.router, prefix="/planner", tags=["Planner"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"], # Vite's default port
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health", tags=["System"])
def health_check():
    return {"status": "online", "framework": settings.PROJECT_NAME}