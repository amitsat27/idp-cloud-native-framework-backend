from fastapi import FastAPI, Request
from app.core.config import settings
from app.api.routes import intent, cluster, plan, auth, admin
from app.core.auth import verify_token_middleware
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title=settings.PROJECT_NAME)

# ✨ Add CORS middleware FIRST (must be before auth to handle errors properly)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],  # Vite's default port + alternatives
    allow_credentials=True,  # ✨ Required for cookie handling
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✨ Add authentication middleware (runs after CORS)
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    return await verify_token_middleware(request, call_next)

# Register authentication and admin routes
app.include_router(auth.router)
app.include_router(admin.router)

# Register existing routes
app.include_router(intent.router, prefix="/api/v1/intent", tags=["Orchestration"])
app.include_router(cluster.router, prefix="/api/v1/cluster", tags=["Cluster"])
app.include_router(plan.router, prefix="/planner", tags=["Planner"])

@app.get("/health", tags=["System"])
def health_check():
    return {"status": "online", "framework": settings.PROJECT_NAME}