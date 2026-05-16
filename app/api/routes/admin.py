"""Admin routes for user management"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
import logging
from app.services.user_service import user_service
from app.core.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# ==================== Models ====================

class ApproveUserRequest(BaseModel):
    pass

class RejectUserRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)

class DeactivateUserRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)

class PromoteToAdminRequest(BaseModel):
    pass

class UpdateUserRoleRequest(BaseModel):
    role: str = Field(..., description="New role: VIEWER, OPERATOR, or ADMIN")
    
    @field_validator("role")
    def validate_role(cls, v):
        if v not in ["VIEWER", "OPERATOR", "ADMIN"]:
            raise ValueError("Role must be VIEWER, OPERATOR, or ADMIN")
        return v

# ==================== Pending Requests ====================

@router.get("/requests")
async def get_pending_requests(request: Request):
    """Get all pending signup requests (admin-only)"""
    # Auth middleware already checked JWT and status=ACTIVE
    # require_admin decorator will check role
    if request.state.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        requests = user_service.list_pending_requests(limit=100)
        # Remove password hashes
        for req in requests:
            req.pop("password_hash", None)
        return {
            "status": "success",
            "count": len(requests),
            "requests": requests
        }
    except Exception as e:
        logger.error(f"Error getting pending requests: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Approve/Reject ====================

@router.patch("/users/{user_id}/approve")
async def approve_user(user_id: str, request: Request):
    """Approve pending user (admin-only)"""
    if request.state.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Check user exists and is PENDING
        user = user_service.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.get("status") != "PENDING":
            raise HTTPException(
                status_code=400,
                detail=f"Can only approve PENDING users. Current status: {user.get('status')}"
            )
        
        # Approve
        user_service.approve_user(user_id, request.state.user_id)
        
        logger.info(f"User {user_id} approved by {request.state.user_id}")
        
        return {
            "status": "success",
            "message": f"User {user_id} approved"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error approving user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/users/{user_id}/reject")
async def reject_user(user_id: str, req: RejectUserRequest, request: Request):
    """Reject pending user (admin-only)"""
    if request.state.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Check user exists and is PENDING
        user = user_service.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.get("status") != "PENDING":
            raise HTTPException(
                status_code=400,
                detail=f"Can only reject PENDING users. Current status: {user.get('status')}"
            )
        
        # Reject
        user_service.reject_user(user_id, req.reason, request.state.user_id)
        
        logger.info(f"User {user_id} rejected by {request.state.user_id}")
        
        return {
            "status": "success",
            "message": f"User {user_id} rejected"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rejecting user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== User Management ====================

@router.get("/users")
async def list_users(request: Request):
    """List all active users (admin-only)"""
    if request.state.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        users = user_service.list_active_users(limit=100)
        return {
            "status": "success",
            "count": len(users),
            "users": users
        }
    except Exception as e:
        logger.error(f"Error listing users: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/users/{user_id}/deactivate")
async def deactivate_user(user_id: str, req: DeactivateUserRequest, request: Request):
    """Deactivate active user (admin-only)"""
    if request.state.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Check user exists and is ACTIVE
        user = user_service.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.get("status") != "ACTIVE":
            raise HTTPException(
                status_code=400,
                detail=f"Can only deactivate ACTIVE users. Current status: {user.get('status')}"
            )
        
        # Deactivate (✨ Instant - future requests will be rejected)
        user_service.deactivate_user(user_id, request.state.user_id)
        
        logger.info(f"User {user_id} deactivated by {request.state.user_id}")
        
        return {
            "status": "success",
            "message": f"User {user_id} deactivated immediately"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deactivating user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/users/{user_id}/promote-admin")
async def promote_to_admin(user_id: str, request: Request):
    """Promote user to ADMIN role (admin-only)"""
    if request.state.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        user = user_service.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.get("role") == "ADMIN":
            raise HTTPException(status_code=400, detail="User is already an admin")
        
        user_service.update_user_role(user_id, "ADMIN")
        
        logger.info(f"User {user_id} promoted to ADMIN by {request.state.user_id}")
        
        return {
            "status": "success",
            "message": f"User {user_id} promoted to ADMIN"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error promoting user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/users/{user_id}/role")
async def update_user_role(user_id: str, req: UpdateUserRoleRequest, request: Request):
    """Update user role to VIEWER, OPERATOR, or ADMIN (admin-only)"""
    if request.state.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Prevent admin from changing their own role
        if user_id == request.state.user_id:
            raise HTTPException(
                status_code=400,
                detail="Cannot change your own role. Ask another admin for help."
            )
        
        # Check user exists and is ACTIVE
        user = user_service.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.get("status") != "ACTIVE":
            raise HTTPException(
                status_code=400,
                detail=f"Can only change role for ACTIVE users. Current status: {user.get('status')}"
            )
        
        old_role = user.get("role")
        if old_role == req.role:
            raise HTTPException(
                status_code=400,
                detail=f"User already has role {req.role}"
            )
        
        # Update role
        user_service.update_user_role(user_id, req.role)
        
        logger.info(f"User {user_id} role changed from {old_role} to {req.role} by {request.state.user_id}")
        
        return {
            "status": "success",
            "message": f"User {user_id} role changed from {old_role} to {req.role}",
            "user_id": user_id,
            "old_role": old_role,
            "new_role": req.role
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating user role: {e}")
        raise HTTPException(status_code=500, detail=str(e))
