from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict

class IntentRequest(BaseModel):
    user_input: str = Field(..., description="The natural language intent")
    dry_run: bool = Field(False)

class GenerateRequest(BaseModel):
    """Request model for /generate endpoint (does not support dry_run)"""
    user_input: str = Field(..., description="The natural language intent")

class NetworkingConfig(BaseModel):
    expose_internally: bool = Field(True, description="Creates a ClusterIP Service")
    expose_externally: bool = Field(False, description="Creates a NodePort Service or Ingress")
    ingress_host: Optional[str] = Field(None, description="The domain name if an Ingress is required")

class ScalingConfig(BaseModel):
    """Configuration for Horizontal Pod Autoscaler (HPA)"""
    min_replicas: int = Field(1, ge=1)
    max_replicas: int = Field(5, le=50)
    cpu_threshold_percent: int = Field(80, ge=10, le=95)

class ExecutionPlan(BaseModel):
    app_name: str = Field(...)
    image: Optional[str] = Field(None)
    replicas: int = Field(1)

    service_port: Optional[int] = Field(None)
    container_port: Optional[int] = Field(None)

    target_namespace: str = Field("default", description="The namespace identified by the AI")

    action: str = Field(..., pattern="^(create|update|delete|create_namespace|delete_namespace|no_action|ambiguous)$")

    networking: Optional[NetworkingConfig] = None
    autoscaling: Optional[ScalingConfig] = None # Support for HPA
    storage_gb: Optional[int] = Field(None, description="Size of persistent storage in GB")
    config_data: Optional[Dict[str, str]] = Field(
        None,
        description="Dictionary of filenames and their contents for ConfigMap"
    )
    secret_data: Optional[Dict[str, str]] = Field(
        None,
        description="Dictionary of key-value pairs for Secret"
    )
    resources: Optional[Dict[str, Dict[str, str]]] = Field(
        None,
        description="K8s resource requirements: {requests: {cpu, memory}, limits: {cpu, memory}}"
    )

    resource_kind: str = Field(
        "deployment",
        description="K8s resource type: 'deployment' for stateless apps, 'statefulset' for stateful apps (databases, caches)"
    )

    reasoning: str = Field(...)
    summary: str = Field(..., description="A plain-English explanation of this specific resource action")

    @field_validator('app_name', 'target_namespace')
    @classmethod
    def normalize_names(cls, v: str):
        """Ensures Kubernetes compatibility by forcing lowercase and hyphens."""
        if v:
            return v.lower().replace(" ", "-").replace("_", "-").strip()
        return v

class OrchestrationPlan(BaseModel):
    """
    Parent wrapper to handle multiple execution steps in a single AI response.
    Crucial for intents like 'create 3 namespaces and deploy apps'.
    """
    overall_summary: str = Field(..., description="High-level description of all combined actions")
    plans: List[ExecutionPlan] = Field(..., description="List of individual K8s resource plans")