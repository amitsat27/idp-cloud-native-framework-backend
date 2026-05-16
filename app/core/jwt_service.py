"""JWT token generation and verification"""
import jwt
import time
from datetime import datetime, timedelta
from app.core.config import settings
from fastapi import HTTPException

class JWTService:
    """Stateless JWT token service"""
    
    @staticmethod
    def generate_token(user_id: str, username: str, role: str) -> str:
        """
        Generate JWT token containing user info.
        Token is self-contained and expires automatically.
        """
        now = int(time.time())
        exp = now + (settings.JWT_EXPIRY_HOURS * 3600)
        
        payload = {
            "user_id": user_id,
            "username": username,
            "role": role,
            "iat": now,
            "exp": exp
        }
        
        token = jwt.encode(
            payload,
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM
        )
        
        return token
    
    @staticmethod
    def verify_token(token: str) -> dict:
        """
        Verify JWT signature and expiry.
        Returns decoded payload if valid.
        Raises HTTPException if invalid or expired.
        """
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM]
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")
    
    @staticmethod
    def get_expiry_timestamp() -> int:
        """Get token expiry as epoch timestamp"""
        return int(time.time()) + (settings.JWT_EXPIRY_HOURS * 3600)

# Singleton instance
jwt_service = JWTService()
