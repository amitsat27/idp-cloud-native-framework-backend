import boto3
from app.core.config import settings

def get_bedrock_client():
    """Returns an authenticated Bedrock client using the project's AWS Profile."""
    session = boto3.Session(profile_name=settings.AWS_PROFILE)
    return session.client("bedrock-runtime", region_name=settings.AWS_REGION)