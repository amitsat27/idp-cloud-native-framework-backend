"""Authentication routes: signup, login, setup"""
from fastapi import APIRouter, HTTPException, Response, Request
from pydantic import BaseModel, EmailStr, Field
import bcrypt
import logging
import os
from app.services.user_service import user_service
from app.core.jwt_service import jwt_service
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["auth"])

# ==================== Models ====================

class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8)
    password_confirm: str
    role: str = Field(default="VIEWER")  # VIEWER, OPERATOR, ADMIN

class LoginRequest(BaseModel):
    username: str
    password: str

class CreateAdminRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8)
    password_confirm: str

# ==================== Setup Endpoint ====================

@router.get("/setup")
async def check_setup():
    """Check if setup is needed (no admin exists)"""
    admin_exists = user_service.admin_exists()
    return {
        "setup_needed": not admin_exists,
        "status": "setup_complete" if admin_exists else "setup_required"
    }

@router.post("/setup/create-admin")
async def create_admin(req: CreateAdminRequest, response: Response):
    """
    Create first admin user.
    Only works if no admin exists.
    Returns JWT token in HttpOnly cookie.
    """
    try:
        # ✨ Check if admin already exists
        if user_service.admin_exists():
            raise HTTPException(status_code=400, detail="Admin already exists")
        
        # Validate passwords match
        if req.password != req.password_confirm:
            raise HTTPException(status_code=400, detail="Passwords do not match")
        
        # Check username/email not taken
        if user_service.user_exists(req.username):
            raise HTTPException(status_code=400, detail="Username already taken")
        if user_service.email_exists(req.email):
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Create admin
        user = user_service.create_admin(req.username, req.email, req.password)
        
# Generate JWT token
        token = jwt_service.generate_token(
            user_id=user["user_id"],
            username=user["username"],
            role=user["role"]
        )
        
        # Set HttpOnly cookie (secure only in production, not localhost)
        is_production = os.getenv('ENV') == 'production'
        response.set_cookie(
            key="auth_token",
            value=token,
            httponly=True,
            secure=is_production,  # ✨ True in production, False for localhost
            samesite="lax",  # ✨ "lax" for localhost cross-origin, "strict" for production
            max_age=settings.JWT_EXPIRY_HOURS * 3600  # Convert hours to seconds
        )

        logger.info(f"Admin created: {req.username}")
        
        return {
            "status": "success",
            "message": "Admin created successfully",
            "user_id": user["user_id"],
            "username": user["username"],
            "role": "ADMIN"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating admin: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Signup Endpoint ====================

@router.post("/auth/signup")
async def signup(req: SignupRequest):
    """
    User self-signup: creates PENDING user request with requested role.
    ✨ Admin must approve before user can login.
    """
    try:
        # Validate passwords match
        if req.password != req.password_confirm:
            raise HTTPException(status_code=400, detail="Passwords do not match")
        
        # Validate role - only allow VIEWER, OPERATOR, or ADMIN
        allowed_roles = ["VIEWER", "OPERATOR", "ADMIN"]
        requested_role = req.role.upper() if req.role else "VIEWER"
        if requested_role not in allowed_roles:
            raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(allowed_roles)}")
        
        # Check username/email not taken
        if user_service.user_exists(req.username):
            raise HTTPException(status_code=409, detail="Username already taken")
        if user_service.email_exists(req.email):
            raise HTTPException(status_code=409, detail="Email already registered")
        
        # Create user with requested role and status=PENDING (admin must approve)
        user = user_service.create_user(req.username, req.email, req.password, req.role)
        
        logger.info(f"Signup request: {req.username} - requested role: {req.role}")
        
        return {
            "status": "success",
            "message": "Signup request submitted. Awaiting admin approval.",
            "user_id": user["user_id"],
            "requested_role": req.role
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in signup: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Login Endpoint ====================

@router.post("/auth/login")
async def login(req: LoginRequest, response: Response):
    """
    User login: validates credentials and status=ACTIVE.
    Returns JWT token in HttpOnly cookie.
    """
    try:
        logger.info(f"Login attempt: {req.username}")
        
        # Debug: list all users first
        all_users = user_service.list_all_users()
        logger.warning(f"Debug - All users in DB: {[(u.get('username'), u.get('status')) for u in all_users]}")
        
        # Get user by username
        user = user_service.get_by_username(req.username)
        if not user:
            logger.warning(f"Login failed: User not found - {req.username}")
            raise HTTPException(status_code=401, detail="Invalid username or password")
        
        logger.info(f"User found: {req.username}, status: {user.get('status')}")
        
        # Verify password (convert Binary to bytes if needed)
        password_hash = user["password_hash"]
        if hasattr(password_hash, '__bytes__'):  # DynamoDB Binary object
            password_hash = bytes(password_hash)
        
        password_valid = bcrypt.checkpw(
            req.password.encode(),
            password_hash
        )
        if not password_valid:
            logger.warning(f"Login failed: Invalid password - {req.username}")
            raise HTTPException(status_code=401, detail="Invalid username or password")
        
        logger.info(f"Password verified for: {req.username}")
        
        # Check user status
        if user.get("status") == "PENDING":
            logger.warning(f"Login blocked: User pending approval - {req.username}")
            raise HTTPException(
                status_code=403,
                detail="Your account is pending approval. Please wait for admin approval."
            )
        elif user.get("status") == "INACTIVE":
            logger.warning(f"Login blocked: User inactive - {req.username}")
            raise HTTPException(
                status_code=403,
                detail="Your account has been deactivated. Contact admin for support."
            )
        elif user.get("status") != "ACTIVE":
            logger.warning(f"Login blocked: Invalid status - {req.username} - {user.get('status')}")
            raise HTTPException(status_code=403, detail="Account cannot be used")
        
        logger.info(f"Status check passed for: {req.username}")
        
        # Generate JWT token
        token = jwt_service.generate_token(
            user_id=user["user_id"],
            username=user["username"],
            role=user["role"]
        )
        
        logger.info(f"Token generated for: {req.username}")
        
        # Set HttpOnly cookie (secure only in production, not localhost)
        is_production = os.getenv('ENV') == 'production'
        response.set_cookie(
            key="auth_token",
            value=token,
            httponly=True,
            secure=is_production,  # ✨ True in production, False for localhost
            samesite="lax",  # ✨ "lax" for localhost cross-origin, "strict" for production
            max_age=settings.JWT_EXPIRY_HOURS * 3600  # Convert hours to seconds
        )

        logger.info(f"Login successful: {req.username}")
        
        return {
            "status": "success",
            "user_id": user["user_id"],
            "username": user["username"],
            "role": user["role"]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in login: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/auth/logout")
async def logout(response: Response):
    """Clear authentication cookie"""
    is_production = os.getenv('ENV') == 'production'
    response.delete_cookie(
        key="auth_token",
        httponly=True,
        secure=is_production,
        samesite="lax"
    )
    return {"status": "success", "message": "Logged out"}

@router.get("/auth/me")
async def get_current_user(request: Request):
    """
    Get current authenticated user info.
    ✨ Protected endpoint - requires valid JWT token.
    Returns 401 if not authenticated.
    """
    try:
        # Get token from cookie
        token = request.cookies.get("auth_token")
        if not token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        # Verify token
        payload = jwt_service.verify_token(token)
        
        return {
            "user_id": payload.get("user_id"),
            "username": payload.get("username"),
            "role": payload.get("role")
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_current_user: {e}")
        raise HTTPException(status_code=401, detail="Not authenticated")
