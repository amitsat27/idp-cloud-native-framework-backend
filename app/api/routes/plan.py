from fastapi import APIRouter, HTTPException
from app.models.schemas import GenerateRequest, OrchestrationPlan
from app.services.planner import OrchestrationPlanner
from app.services.scanner import ClusterScanner
from app.services.compiler import SynthesisEngine
from app.services.inspector import ImageInspector
import time
import json

router = APIRouter()
planner_service = OrchestrationPlanner()
scanner_service = ClusterScanner()
compiler = SynthesisEngine()
image_inspector = ImageInspector()

@router.post("/generate")
async def generate_orchestration_plan(request: GenerateRequest):
    try:
        context = scanner_service.get_cluster_state()
        orchestration, token_usage = planner_service.generate_plan(request.user_input, context)

        # Validate images before proceeding
        image_errors = []
        for plan in orchestration.plans:
            if plan.image and plan.action in ["create", "update"]:
                result = image_inspector.validate_image_exists(plan.image)
                if not result["valid"]:
                    image_errors.append(result)

        if image_errors:
            return {
                "status": "image_validation_failed",
                "message": "Some images could not be found.",
                "image_errors": image_errors,
                "suggestions": [
                    "Check the spelling of the image name",
                    "Use well-known images like: nginx, redis, postgres, mysql, alpine",
                    "Specify a full image path with tag (e.g., 'nginx:1.25')"
                ]
            }

        # Parse cluster metrics for capacity-aware decisions
        try:
            cluster_metrics = json.loads(context) if isinstance(context, str) else context
        except Exception:
            cluster_metrics = None

        results = []
        categorized_manifests = {} # Changed from list to dict

        for plan in orchestration.plans:
            final_namespace = plan.target_namespace or "default"
            result = compiler.compile_and_apply(plan, namespace=final_namespace, dry_run=True, cluster_metrics=cluster_metrics)
            results.append(result)
            if "manifests" in result:
                categorized_manifests.update(result["manifests"]) # Use .update()

        return {
            "status": "success",
            "usage": token_usage,
            "orchestration_plan": orchestration.model_dump(),
            "execution": {
                "results": results,
                "manifests": categorized_manifests
            }
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))