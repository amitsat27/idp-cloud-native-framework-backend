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

    # Instructs Pydantic to read the .env file at the root level
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Instantiate for use in other modules
settings = Settings()