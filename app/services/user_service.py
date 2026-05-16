"""User management service - CRUD operations"""
import uuid
import time
import bcrypt
import logging
from datetime import datetime
from botocore.exceptions import ClientError
from app.core.client import db_client

logger = logging.getLogger(__name__)

class UserService:
    """Handles all user operations with DynamoDB"""
    
    def __init__(self):
        self.table = db_client.get_users_table()
    
    def admin_exists(self) -> bool:
        """Check if any ADMIN user exists"""
        try:
            response = self.table.scan(
                FilterExpression="attribute_exists(#role) AND #role = :role",
                ExpressionAttributeNames={"#role": "role"},
                ExpressionAttributeValues={":role": "ADMIN"},
                Limit=1
            )
            return response['Count'] > 0
        except Exception as e:
            logger.error(f"Error checking admin existence: {e}")
            return False
    
    def user_exists(self, username: str) -> bool:
        """Check if username already taken"""
        try:
            response = self.table.scan(
                FilterExpression="username = :username",
                ExpressionAttributeValues={":username": username},
                Limit=1
            )
            return response['Count'] > 0
        except Exception as e:
            logger.error(f"Error checking username: {e}")
            return False
    
    def email_exists(self, email: str) -> bool:
        """Check if email already registered"""
        try:
            response = self.table.scan(
                FilterExpression="email = :email",
                ExpressionAttributeValues={":email": email},
                Limit=1
            )
            return response['Count'] > 0
        except Exception as e:
            logger.error(f"Error checking email: {e}")
            return False
    
    def get_user(self, user_id: str) -> dict:
        """Get user by user_id"""
        try:
            response = self.table.get_item(Key={"user_id": user_id})
            return response.get("Item")
        except Exception as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None
    
    def get_by_username(self, username: str) -> dict:
        """Get user by username"""
        try:
            # Use scan to find by username attribute
            response = self.table.scan(
                FilterExpression="username = :username",
                ExpressionAttributeValues={":username": username},
                Limit=10
            )
            logger.warning(f"Scan for '{username}': {response.get('Count')} items found")
            if response['Count'] > 0:
                return response['Items'][0]
            return None
        except Exception as e:
            logger.error(f"Error getting user by username {username}: {e}")
            return None
    
    def create_admin(self, username: str, email: str, password: str) -> dict:
        """
        Create first admin user.
        Uses conditional write to prevent race condition.
        """
        try:
            user_id = str(uuid.uuid4())
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12))
            now = int(time.time())
            
            # Conditional write: fails if ADMIN already exists
            self.table.put_item(
                Item={
                    "user_id": user_id,
                    "username": username,
                    "email": email,
                    "password_hash": password_hash,
                    "role": "ADMIN",
                    "status": "ACTIVE",
                    "created_at": now
                },
                ConditionExpression="attribute_not_exists(#role) OR #role <> :admin",
                ExpressionAttributeNames={"#role": "role"},
                ExpressionAttributeValues={":admin": "ADMIN"}
            )
            
            logger.info(f"Admin created: {username}")
            return {
                "user_id": user_id,
                "username": username,
                "email": email,
                "role": "ADMIN",
                "status": "ACTIVE",
                "created_at": now
            }
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise Exception("Admin already exists")
            logger.error(f"Error creating admin: {e}")
            raise
    
    def create_user(self, username: str, email: str, password: str, role: str = "VIEWER") -> dict:
        """
        Create new user with requested role and status=PENDING.
        Roles: VIEWER (default), OPERATOR, ADMIN
        Admin must approve before user can login.
        """
        try:
            user_id = str(uuid.uuid4())
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12))
            now = int(time.time())
            
            # Validate role
            valid_roles = ["VIEWER", "OPERATOR", "ADMIN"]
            user_role = role.upper() if role and role.upper() in valid_roles else "VIEWER"
            
            self.table.put_item(Item={
                "user_id": user_id,
                "username": username,
                "email": email,
                "password_hash": password_hash,
                "role": user_role,
                "status": "PENDING",
                "created_at": now
            })
            
            logger.info(f"User signup request: {username} - role: {user_role}")
            return {
                "user_id": user_id,
                "username": username,
                "email": email,
                "role": user_role,
                "status": "PENDING",
                "created_at": now
            }
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            raise
    
    def approve_user(self, user_id: str, approved_by: str) -> dict:
        """Approve pending user (status: PENDING → ACTIVE)"""
        try:
            now = int(time.time())
            
            self.table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET #status = :active, approved_at = :now, approved_by = :admin",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":active": "ACTIVE",
                    ":now": now,
                    ":admin": approved_by
                },
                ReturnValues="ALL_NEW"
            )
            
            logger.info(f"User {user_id} approved by {approved_by}")
            return {"status": "success", "message": f"User {user_id} approved"}
        except Exception as e:
            logger.error(f"Error approving user: {e}")
            raise
    
    def reject_user(self, user_id: str, reason: str, rejected_by: str) -> dict:
        """Reject pending user (status: PENDING → INACTIVE)"""
        try:
            now = int(time.time())
            
            self.table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET #status = :inactive, rejected_at = :now, rejection_reason = :reason, rejected_by = :admin",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":inactive": "INACTIVE",
                    ":now": now,
                    ":reason": reason,
                    ":admin": rejected_by
                },
                ReturnValues="ALL_NEW"
            )
            
            logger.info(f"User {user_id} rejected by {rejected_by}")
            return {"status": "success", "message": f"User {user_id} rejected"}
        except Exception as e:
            logger.error(f"Error rejecting user: {e}")
            raise
    
    def deactivate_user(self, user_id: str, deactivated_by: str) -> dict:
        """Deactivate active user (status: ACTIVE → INACTIVE)"""
        try:
            now = int(time.time())
            
            self.table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET #status = :inactive, deactivated_at = :now, deactivated_by = :admin",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":inactive": "INACTIVE",
                    ":now": now,
                    ":admin": deactivated_by
                },
                ReturnValues="ALL_NEW"
            )
            
            logger.info(f"User {user_id} deactivated by {deactivated_by}")
            return {"status": "success", "message": f"User {user_id} deactivated"}
        except Exception as e:
            logger.error(f"Error deactivating user: {e}")
            raise
    
    def update_user_role(self, user_id: str, new_role: str) -> dict:
        """Update user role (ADMIN/USER)"""
        try:
            now = int(time.time())
            
            self.table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET #role = :role, updated_at = :now",
                ExpressionAttributeNames={"#role": "role"},
                ExpressionAttributeValues={
                    ":role": new_role,
                    ":now": now
                },
                ReturnValues="ALL_NEW"
            )
            
            logger.info(f"User {user_id} role updated to {new_role}")
            return {"status": "success", "message": f"User role updated to {new_role}"}
        except Exception as e:
            logger.error(f"Error updating user role: {e}")
            raise
    
    def list_pending_requests(self, limit: int = 10) -> list:
        """Get all pending signup requests"""
        try:
            response = self.table.scan(
                FilterExpression="#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":pending": "PENDING"},
                Limit=limit
            )
            return response.get("Items", [])
        except Exception as e:
            logger.error(f"Error listing pending requests: {e}")
            return []
    
    def list_active_users(self, limit: int = 100) -> list:
        """Get all active users"""
        try:
            response = self.table.scan(
                FilterExpression="#status = :active",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":active": "ACTIVE"},
                Limit=limit
            )
            # Remove password hashes from response
            items = response.get("Items", [])
            for item in items:
                item.pop("password_hash", None)
            return items
        except Exception as e:
            logger.error(f"Error listing active users: {e}")
            return []
    
    def list_all_users(self, limit: int = 100) -> list:
        """Get all users (debug)"""
        try:
            response = self.table.scan(Limit=limit)
            return response.get("Items", [])
        except Exception as e:
            logger.error(f"Error listing users: {e}")
            return []

# Singleton instance
user_service = UserService()
