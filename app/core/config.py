from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class Settings(BaseSettings):
    PROJECT_NAME: str = "IDP Framework"
    
    # AWS Bedrock Config
    AWS_PROFILE: str
    AWS_REGION: str
    BEDROCK_MODEL_ID: str
    
    # Minikube/K8s Config
    K8S_NAMESPACE: str = os.getenv("K8S_NAMESPACE", "default")
    
    # JWT Configuration
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "your-super-secret-key-min-32-characters")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_EXPIRY_HOURS: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
    
    # DynamoDB Configuration
    DYNAMODB_TABLE_NAME: str = os.getenv("DYNAMODB_TABLE_NAME", "idp-users")

    # Instructs Pydantic to read the .env file at the root level
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Instantiate for use in other modules
settings = Settings()