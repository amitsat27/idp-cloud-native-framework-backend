from fastapi import APIRouter, HTTPException
from app.models.schemas import IntentRequest
from app.services.scanner import ClusterScanner
from app.services.planner import OrchestrationPlanner
from app.services.compiler import SynthesisEngine
from app.services.validate_resources import ResourceValidator
from app.services.inspector import ImageInspector
from kubernetes import client
import time
import json
import yaml

router = APIRouter()
scanner = ClusterScanner()
planner = OrchestrationPlanner()
compiler = SynthesisEngine()
image_inspector = ImageInspector()

@router.post("/process-intent")
def process_user_intent(request: IntentRequest):
    start_total = time.time()
    try:
        cluster_context = scanner.get_cluster_state()

        # Phase 1: Planning (AI inference)
        start_planning = time.time()
        orchestration, token_usage = planner.generate_plan(request.user_input, cluster_context)
        planning_time_sec = time.time() - start_planning

        # Check if intent was ambiguous
        if orchestration.plans and orchestration.plans[0].action == "ambiguous":
            # Distinguish between generic ambiguity and specific validation errors
            ambiguous_app = orchestration.plans[0].app_name
            if ambiguous_app == "invalid_replicas":
                return {
                    "intent_received": request.user_input,
                    "status": "ambiguous_intent",
                    "message": "Invalid replica count. A deployment must have at least 1 replica.",
                    "suggestions": [
                        "Specify a positive number of replicas (e.g., 'deploy httpd with 3 replicas')",
                        "Use at least 1 replica for any deployment",
                        "Or remove the replica count entirely for a default of 1"
                    ],
                    "example_intents": [
                        "Deploy httpd with 3 replicas",
                        "Deploy nginx with 1 replica",
                        "Deploy httpd"
                    ]
                }
            if ambiguous_app == "invalid_port":
                return {
                    "intent_received": request.user_input,
                    "status": "ambiguous_intent",
                    "message": "Invalid port number. Port must be between 1 and 65535.",
                    "suggestions": [
                        "Specify a port between 1 and 65535 (e.g., 80, 443, 8080, 3000)",
                        "Port 0 is reserved by the system",
                        "Ports 6443, 2379, 2380, 10250-10259 are reserved for K8s control plane"
                    ],
                    "example_intents": [
                        "Deploy nginx with 3 replicas on port 80",
                        "Deploy httpd on port 8080",
                        "Deploy postgres with 1 replica"
                    ]
                }

        # Post-plan validation: catch any create/update action with replicas < 1
        for plan in orchestration.plans:
            if plan.action in ["create", "update"] and plan.replicas < 1:
                return {
                    "intent_received": request.user_input,
                    "status": "ambiguous_intent",
                    "message": "Invalid replica count. A deployment must have at least 1 replica.",
                    "suggestions": [
                        "Specify a positive number of replicas (e.g., 'deploy httpd with 3 replicas')",
                        "Use at least 1 replica for any deployment",
                        "Or remove the replica count entirely for a default of 1"
                    ],
                    "example_intents": [
                        "Deploy httpd with 3 replicas",
                        "Deploy nginx with 1 replica",
                        "Deploy httpd"
                    ]
                }

            # Post-plan validation: catch ports outside 1-65535, reserved, or NodePort as container_port
            if plan.action in ["create", "update"]:
                for port_field in ["service_port", "container_port"]:
                    port_val = getattr(plan, port_field, None)
                    if port_val is not None:
                        if port_val < 1 or port_val > 65535:
                            return {"intent_received": request.user_input, "status": "ambiguous_intent",
                                    "message": f"Invalid port {port_val}. Port must be between 1 and 65535.",
                                    "suggestions": ["Specify a port number between 1 and 65535", "For web servers, use port 80", "For databases, use the standard port (e.g., 5432 for postgres)"],
                                    "example_intents": ["Deploy nginx with 3 replicas on port 80", "Deploy httpd on port 8080", "Deploy postgres with 1 replica"]}
                        if port_val == 0:
                            return {"intent_received": request.user_input, "status": "ambiguous_intent",
                                    "message": "Port 0 is reserved by the system and cannot be used.",
                                    "suggestions": ["Specify a non-zero port number", "For web servers, use port 80", "For databases, use the standard port (e.g., 5432 for postgres)"],
                                    "example_intents": ["Deploy nginx with 3 replicas on port 80", "Deploy httpd on port 8080", "Deploy postgres with 1 replica"]}
                        if port_field == "container_port" and 30000 <= port_val <= 32767:
                            return {"intent_received": request.user_input, "status": "ambiguous_intent",
                                    "message": f"Port {port_val} is in the NodePort range (30000-32767) and cannot be used as a container port.",
                                    "suggestions": ["Use a standard application port for the container (e.g., 80 for web servers)", "If you need NodePort, specify it as a service port instead"],
                                    "example_intents": ["Deploy nginx with 3 replicas on service port 30080", "Deploy httpd on port 80"]}

        if orchestration.plans and orchestration.plans[0].action == "ambiguous":
            return {
                "intent_received": request.user_input,
                "status": "ambiguous_intent",
                "message": "I couldn't understand your intent clearly. Please be more specific.",
                "suggestions": [
                    "Specify the app/service name (e.g., 'nginx', 'postgresql')",
                    "Specify the action (create, deploy, update, delete, or scale)",
                    "Mention the target namespace if not 'default'",
                    "Provide specific parameters like number of replicas, ports, or storage"
                ],
                "example_intents": [
                    "Deploy nginx with 3 replicas",
                    "Create a PostgreSQL database in the prod namespace",
                    "Scale mysql to 5 replicas",
                    "Delete the old-app from staging namespace"
                ]
            }

        # Parse cluster metrics for capacity-aware decisions
        try:
            cluster_metrics = json.loads(cluster_context) if isinstance(cluster_context, str) else cluster_context
        except Exception:
            cluster_metrics = None

        # -- IMAGE VALIDATION --
        # Check all images exist before proceeding to compilation
        image_errors = []
        for plan in orchestration.plans:
            if plan.image and plan.action in ["create", "update"]:
                result = image_inspector.validate_image_exists(plan.image)
                if not result["valid"]:
                    image_errors.append(result)

        if image_errors:
            error_details = "; ".join([e["message"] for e in image_errors])
            return {
                "intent_received": request.user_input,
                "status": "image_validation_failed",
                "message": "Some images could not be found. Please verify the image names and try again.",
                "image_errors": image_errors,
                "suggestions": [
                    "Check the spelling of the image name",
                    "Use well-known images like: nginx, redis, postgres, mysql, alpine",
                    "Specify a full image path with tag (e.g., 'nginx:1.25')",
                    "Ensure the image exists on Docker Hub or your configured registry"
                ]
            }

        v1 = client.CoreV1Api()
        results_list = []
        categorized_manifests = {} # Global dict for all resources
        deep_inspection_total_sec = 0.0

        # Phase 2: Compilation (manifest generation + deep inspection)
        start_compilation = time.time()
        for plan in orchestration.plans:
            # Skip processing for "no_action" plans (already present)
            if plan.action == "no_action":
                results_list.append({
                    "status": "skipped",
                    "reason": f"{plan.app_name} in {plan.target_namespace or 'default'} already exists",
                    "execution_logs": [f"No changes needed for {plan.app_name}"]
                })
                continue
            
            final_namespace = plan.target_namespace or "default"
            creation_log = None

            # CHECK CLUSTER CAPACITY BEFORE DEPLOYMENT
            capacity_check = compiler.check_cluster_capacity(plan)
            capacity_warnings = capacity_check.get("warnings", [])
            
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

            # Accumulate deep inspection time from compiler
            if "_debug_timing" in execution_result:
                deep_inspection_total_sec += execution_result["_debug_timing"]["deep_inspection_sec"]

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
                        print(e)

            if creation_log and "execution_logs" in execution_result:
                execution_result["execution_logs"].insert(0, creation_log)

            # ADD CAPACITY WARNINGS TO RESULT
            if capacity_warnings:
                if "warnings" not in execution_result:
                    execution_result["warnings"] = []
                execution_result["warnings"].extend(capacity_warnings)
            
            # Add capacity info to execution logs
            if not capacity_check.get("capacity_sufficient") and execution_result.get("status") != "skipped":
                execution_result["execution_logs"].append(
                    f"⚠️  Cluster Status: {capacity_check.get('available_cpu_cores', 0):.1f} CPU cores, "
                    f"{capacity_check.get('available_memory_gi', 0):.1f}Gi memory available"
                )

            results_list.append(execution_result)

        compilation_time_sec = time.time() - start_compilation

        # Phase 3: Validation (wait for readiness)
        validation_time_sec = 0.0
        image_pull_completed = False
        if not request.dry_run and categorized_manifests:
            start_validation = time.time()
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
                    delete_manifest_keys.add(f"{ns}-StatefulSet")
                    delete_manifest_keys.add(f"{ns}-HeadlessService")
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
                    if kind in ["Deployment", "StatefulSet", "PersistentVolumeClaim"]:
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
                
                # Phase 3a: Wait for images to be pulled first
                all_images_pulled = True
                deployment_count = 0
                for ns, resources in by_namespace.items():
                    # Filter for only Deployment/StatefulSet resources (images are in pods)
                    deployment_resources = [r for r in resources if r["kind"] in ("Deployment", "StatefulSet")]
                    if deployment_resources:
                        deployment_count += len(deployment_resources)
                        image_pull_success = validator.wait_for_image_pull(deployment_resources, ns)
                        if not image_pull_success:
                            all_images_pulled = False
                            # Continue to check readiness anyway, but log the warning
                
                if deployment_count > 0:
                    image_pull_completed = True
                
                # Phase 3b: Check resource readiness
                all_ready = True
                failure_reasons = []
                for ns, resources in by_namespace.items():
                    results = validator.verify_readiness(resources, ns)
                    for res_status in results:
                        if res_status["status"] != "Ready":
                            all_ready = False
                            # Collect failure reasons for better error messages
                            if res_status.get("status") == "FailedScheduling":
                                if "failure_reasons" in res_status:
                                    failure_reasons.extend(res_status["failure_reasons"])
                                elif "pod_statuses" in res_status:
                                    pod_info = ", ".join([f"{p['pod']}({p['phase']})" for p in res_status["pod_statuses"]])
                                    failure_reasons.append(f"{res_status['resource']}: {pod_info}")
                        if not all_ready:
                            break
                    if not all_ready:
                        break

                if not all_ready:
                    error_msg = "Some resources did not become ready within the timeout period."
                    if failure_reasons:
                        error_msg += " Reasons:\n" + "\n".join(failure_reasons)
                    else:
                        error_msg += " Please check cluster status."
                    raise HTTPException(
                        status_code=400,
                        detail=error_msg
                    )

            validation_time_sec = time.time() - start_validation

        total_time_sec = time.time() - start_total

        # Collect all warnings from individual results
        all_warnings = []
        for result in results_list:
            if "warnings" in result:
                all_warnings.extend(result["warnings"])
        
        response = {
            "intent_received": request.user_input,
            "status": "success",
            "orchestration_plan": orchestration.model_dump(),
            "usage": token_usage,
            "execution": {
                "results": results_list,
                "manifests": categorized_manifests # Returns the full dictionary
            },
            "image_pull_completed": image_pull_completed,
            "execution_time_seconds": round(total_time_sec, 2),
            "timing_breakdown": {
                "planning_seconds": round(planning_time_sec, 2),
                "compilation_seconds": round(compilation_time_sec, 2),
                "deep_inspection_seconds": round(deep_inspection_total_sec, 2),
                "validation_seconds": round(validation_time_sec, 2),
                "total_seconds": round(total_time_sec, 2)
            }
        }
        
        # Add capacity warnings to response if any
        if all_warnings:
            response["capacity_warnings"] = all_warnings
        
        return response
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))