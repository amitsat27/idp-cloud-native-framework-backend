from fastapi import APIRouter, HTTPException
from app.models.schemas import IntentRequest
from app.services.scanner import ClusterScanner
from app.services.planner import OrchestrationPlanner
from app.services.compiler import SynthesisEngine
from app.services.validate_resources import ResourceValidator
from kubernetes import client
import time
import json
import yaml

router = APIRouter()
scanner = ClusterScanner()
planner = OrchestrationPlanner()
compiler = SynthesisEngine()

@router.post("/process-intent")
def process_user_intent(request: IntentRequest):
    start_time = time.time()
    try:
        cluster_context = scanner.get_cluster_state()
        orchestration, token_usage = planner.generate_plan(request.user_input, cluster_context)

        # Parse cluster metrics for capacity-aware decisions
        try:
            cluster_metrics = json.loads(cluster_context) if isinstance(cluster_context, str) else cluster_context
        except Exception:
            cluster_metrics = None
        
        v1 = client.CoreV1Api()
        results_list = []
        categorized_manifests = {} # Global dict for all resources

        for plan in orchestration.plans:
            final_namespace = plan.target_namespace or "default"
            creation_log = None

            # Handle Namespace Actions
            if plan.action == "create_namespace":
                try:
                    if not request.dry_run:
                        v1.create_namespace(body=client.V1Namespace(metadata=client.V1ObjectMeta(name=final_namespace)))
                        creation_log = f"Namespace '{final_namespace}' created."
                except client.exceptions.ApiException as e:
                    if e.status != 409: raise e

            # Run Compiler (Applies resources and returns manifests)
            execution_result = compiler.compile_and_apply(plan, final_namespace, request.dry_run, cluster_metrics)

            # Accumulate manifests using .update() to preserve all namespaces
            if "manifests" in execution_result:
                categorized_manifests.update(execution_result["manifests"])

            if plan.action == "delete_namespace":
                if request.dry_run:
                    try:
                        v1.read_namespace(name=final_namespace)
                        creation_log = f"Dry-run: Would delete namespace '{final_namespace}'"
                    except client.exceptions.ApiException as e:
                        if e.status == 404:
                            creation_log = f"Dry-run: Would skip namespace '{final_namespace}' (not found)"
                        else:
                            creation_log = f"Dry-run: Could not check namespace '{final_namespace}': {str(e)}"
                else:
                    try:
                        v1.read_namespace(name=final_namespace)
                        v1.delete_namespace(name=final_namespace)
                        creation_log = f"Namespace '{final_namespace}' deletion initiated."
                    except client.exceptions.ApiException as e:
                        if e.status == 404:
                            creation_log = f"Namespace '{final_namespace}' not found, skipping deletion"
                        else:
                            creation_log = f"Error deleting namespace '{final_namespace}': {str(e)}"
                    except Exception as e:
                        creation_log = f"Error deleting namespace '{final_namespace}': {str(e)}"

            if creation_log and "execution_logs" in execution_result:
                execution_result["execution_logs"].insert(0, creation_log)
            
            results_list.append(execution_result)

        # Wait for resources to become ready (only for actual execution, not dry-run)
        if not request.dry_run and categorized_manifests:
            # Build set of manifest keys that are part of delete operations
            # This accounts for all resources (Deployment, PVC, Service, etc.) associated with deletion
            delete_manifest_keys = set()
            for plan in orchestration.plans:
                if plan.action == "delete":
                    ns = plan.target_namespace or "default"
                    # All associated resources get this namespace prefix
                    delete_manifest_keys.add(f"{ns}-Deployment")
                    delete_manifest_keys.add(f"{ns}-PersistentVolumeClaim")
                    delete_manifest_keys.add(f"{ns}-Service")
                    delete_manifest_keys.add(f"{ns}-Ingress")
                    delete_manifest_keys.add(f"{ns}-HorizontalPodAutoscaler")
                    delete_manifest_keys.add(f"{ns}-ConfigMap")
                elif plan.action == "delete_namespace":
                    ns = plan.target_namespace or "default"
                    delete_manifest_keys.add(f"{ns}-Namespace")

            resources_to_wait = []
            for key, yaml_str in categorized_manifests.items():
                try:
                    doc = yaml.safe_load(yaml_str)
                    kind = doc["kind"]
                    # Skip resources that are being deleted
                    if key in delete_manifest_keys:
                        continue
                    # Only wait for resources that need to be ready
                    if kind in ["Deployment", "PersistentVolumeClaim"]:
                        resources_to_wait.append({"namespace": doc["metadata"].get("namespace", "default"), "kind": kind, "name": doc["metadata"]["name"]})
                except Exception:
                    continue

            if resources_to_wait:
                # Group by namespace
                by_namespace = {}
                for res in resources_to_wait:
                    ns = res["namespace"]
                    by_namespace.setdefault(ns, []).append({"kind": res["kind"], "name": res["name"]})

                validator = ResourceValidator(
                    compiler.apps_v1, compiler.core_v1,
                    compiler.autoscaling_v2, compiler.networking_v1
                )
                all_ready = True
                for ns, resources in by_namespace.items():
                    results = validator.verify_readiness(resources, ns)
                    for res_status in results:
                        if res_status["status"] != "Ready":
                            all_ready = False
                            break
                    if not all_ready:
                        break

                if not all_ready:
                    raise HTTPException(
                        status_code=400,
                        detail="Some resources did not become ready within the timeout period. Please check cluster status."
                    )

        return {
            "intent_received": request.user_input,
            "status": "success",
            "orchestration_plan": orchestration.model_dump(),
            "usage": token_usage,
            "execution": {
                "results": results_list,
                "manifests": categorized_manifests # Returns the full dictionary
            },
            "execution_time_seconds": round(time.time() - start_time, 2)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))