"""Authentication middleware and utilities"""
import logging
from fastapi import Request, HTTPException
from app.core.jwt_service import jwt_service
from app.core.client import db_client
from functools import wraps

logger = logging.getLogger(__name__)

# Public routes that don't require authentication
PUBLIC_ROUTES = {
    "/api/v1/setup",
    "/api/v1/auth/signup",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/me",
    "/api/v1/setup/create-admin",
    "/health"
}

# Admin-only routes (require authentication + ADMIN role)
ADMIN_ONLY_ROUTES = {
    "/docs",
    "/openapi.json",
    "/redoc"
}

async def verify_token_middleware(request: Request, call_next):
    """
    Middleware to verify JWT token and check user status in DB.
    Ensures instant deactivation works (DB status check on each request)
    
    Admin-only routes (/docs, /openapi.json) require ADMIN role
    """
    
    path = request.url.path
    method = request.method
    
    # Allow OPTIONS preflight requests without auth
    if method == "OPTIONS":
        return await call_next(request)
    
    # SKIP auth completely for public routes - let them handle their own auth
    
    # ✨ SKIP auth completely for public routes - let them handle their own auth
    if path in PUBLIC_ROUTES:
        return await call_next(request)
    
    # Additional public path prefixes (only specific paths, not all paths starting with /)
    public_prefixes = ("/health", "/docs", "/redoc", "/openapi")
    if path.startswith(public_prefixes):
        return await call_next(request)
    
    # Check if route is admin-only
    is_admin_only = path in ADMIN_ONLY_ROUTES or path.startswith(("/redoc", "/docs", "/openapi"))
    
    try:
        # Step 1: Extract token from HttpOnly cookie
        token = request.cookies.get("auth_token")
        logger.info(f"Path: {path}, Cookie token: {'present' if token else 'MISSING'}")
        if not token:
            raise HTTPException(status_code=401, detail="Missing authentication token")
        
        # Step 2: Verify JWT signature and expiry
        payload = jwt_service.verify_token(token)
        
        # Step 3: Extract user_id and role from token
        user_id = payload.get("user_id")
        user_role = payload.get("role")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # Step 4: Check if admin-only route AND user is not ADMIN
        if is_admin_only and user_role != "ADMIN":
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Step 5: Query DynamoDB for user (✨ Instant deactivation check!)
        users_table = db_client.get_users_table()
        response = users_table.get_item(Key={"user_id": user_id})
        
        if "Item" not in response:
            raise HTTPException(status_code=401, detail="User not found")
        
        user = response["Item"]
        
        # Step 6: Check user is ACTIVE (✨ CRITICAL for instant deactivation)
        if user.get("status") != "ACTIVE":
            logger.warning(f"User {user_id} is {user.get('status')}")
            raise HTTPException(
                status_code=403,
                detail=f"User account {user.get('status')}"
            )
        
        # Step 7: Store in request state for use in routes
        request.state.user_id = user_id
        request.state.username = payload.get("username")
        request.state.role = payload.get("role")
        request.state.user = user
        
        # Step 8: Continue to next middleware/route
        response = await call_next(request)
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Auth middleware error: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed")

def require_admin(func):
    """Decorator: Only ADMIN role allowed"""
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not hasattr(request.state, 'role') or request.state.role != "ADMIN":
            raise HTTPException(status_code=403, detail="Admin access required")
        return await func(request, *args, **kwargs)
    return wrapper

def require_role(*allowed_roles):
    """Decorator: Check if user has one of the allowed roles"""
    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            if not hasattr(request.state, 'role') or request.state.role not in allowed_roles:
                raise HTTPException(
                    status_code=403,
                    detail=f"Required role: {', '.join(allowed_roles)}"
                )
            return await func(request, *args, **kwargs)
        return wrapper
    return decorator
