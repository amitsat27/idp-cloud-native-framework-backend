import boto3
from botocore.exceptions import ClientError
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

# ==================== Generic Client Manager ====================

class GenericClient:
    """Unified client manager for AWS services (Bedrock, DynamoDB)"""
    
    def __init__(self):
        self.bedrock_client = self._initialize_bedrock()
        self.dynamodb_resource = self._initialize_dynamodb()
        self.users_table = None
        self._initialize_dynamodb_tables()
    
    def _initialize_bedrock(self):
        """Initialize Bedrock client"""
        try:
            session = boto3.Session(profile_name=settings.AWS_PROFILE)
            client = session.client("bedrock-runtime", region_name=settings.AWS_REGION)
            logger.info("Bedrock client initialized")
            return client
        except Exception as e:
            logger.error(f"Failed to initialize Bedrock client: {e}")
            return None
    
    def _initialize_dynamodb(self):
        """Initialize DynamoDB resource"""
        try:
            session = boto3.Session(profile_name=settings.AWS_PROFILE)
            dynamodb = session.resource(
                'dynamodb',
                region_name=settings.AWS_REGION
            )
            logger.info("DynamoDB resource initialized")
            return dynamodb
        except Exception as e:
            logger.error(f"Failed to initialize DynamoDB: {e}")
            return None
    
    def _initialize_dynamodb_tables(self):
        """Initialize or connect to DynamoDB tables"""
        if not self.dynamodb_resource:
            logger.error("DynamoDB resource not initialized, skipping table initialization")
            return
        
        try:
            self.users_table = self.dynamodb_resource.Table(settings.DYNAMODB_TABLE_NAME)
            self.users_table.load()
            logger.info(f"Connected to {settings.DYNAMODB_TABLE_NAME} table")
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                logger.error(f"Table {settings.DYNAMODB_TABLE_NAME} not found")
                raise Exception(f"DynamoDB table {settings.DYNAMODB_TABLE_NAME} does not exist")
            raise
    
    def get_bedrock_client(self):
        """Get Bedrock client"""
        if not self.bedrock_client:
            raise Exception("Bedrock client not initialized")
        return self.bedrock_client
    
    def get_users_table(self):
        """Get users table reference"""
        if not self.users_table:
            raise Exception("Users table not initialized")
        return self.users_table

# Singleton instance
try:
    client = GenericClient()
    db_client = client  # Alias for backward compatibility
except Exception as e:
    logger.error(f"Failed to initialize GenericClient: {e}")
    raise