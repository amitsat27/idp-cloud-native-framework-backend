import json
import re
from typing import Tuple, Dict
from app.core.client import get_bedrock_client
from app.core.config import settings
from app.models.schemas import OrchestrationPlan, ExecutionPlan

class OrchestrationPlanner:
    def __init__(self):
        self.client = get_bedrock_client()
        self.model_id = settings.BEDROCK_MODEL_ID

    def _get_compact_schema(self) -> str:
        """Returns a high-density structural definition to save tokens."""
        return """
        {
          "overall_summary": "One sentence summary of all actions",
          "plans": [
            {
              "app_name": string,
              "image": string | null,
              "target_namespace": string,
              "replicas": int,
              "service_port": int | null,
              "container_port": int | null,
              "action": "create" | "update" | "delete" | "create_namespace" | "delete_namespace",
              "networking": { "expose_externally": bool, "ingress_host": string | null } | null,
              "storage_gb": int | null,
              "config_data": dict | null,
              "secret_data": dict | null,
              "resources": { "requests": { "cpu": string, "memory": string }, "limits": { "cpu": string, "memory": string } } | null,
              "autoscaling": { "min_replicas": int, "max_replicas": int, "cpu_threshold_percent": int } | null,
              "reasoning": string,
              "summary": string
            }
          ]
        }
        """

    def generate_plan(self, intent: str, context: str) -> Tuple[OrchestrationPlan, Dict]:
        try:
            state_data = json.loads(context)
            existing_apps = [f"{w['name']} in {w['namespace']}" for w in state_data.get('existing_workloads', [])]
            active_namespaces = state_data.get('active_namespaces', [])
        except Exception:
            existing_apps = []
            active_namespaces = []
            active_namespaces = []

        schema_def = self._get_compact_schema()
        
        # PROMPT: Original Rules 1-19 kept. Added Rule 20 and Rule 21.
        prompt = f"""<|begin_of_text|><|start_header_id|>system<|end_header_id|>
        Role: Expert K8s Orchestration Planner
        Task: Map INTENT to JSON based on current cluster STATE.

        STRICT MULTI-RESOURCE RULE:
        1. If the intent involves multiple resources (e.g., 'create namespaces prod and dev'), you MUST provide a separate entry in the "plans" list for EACH resource. Output ONE JSON object matching the OrchestrationPlan schema.

        STRICT ACTION RULES:
        1. If number of replicas is not present make number of repilacs as 1 default.
        2. Identify the 'target_namespace' from user intent (e.g., 'staging', 'production').
        3. IF no namespace is mentioned, YOU MUST set "target_namespace": "default". 
        4. IF the intent is ONLY to create/delete a namespace (e.g., 'create namespace staging'): Use action 'create_namespace', replicas: 0, and image: null.
        5. IF the intent is to delete an app: Set "action": "delete".
        6. Identify the 'app_name' from user intent (e.g., 'nginx-web-server').
        7. Check if the exact 'app_name' exists IN the 'target_namespace' using the CLUSTER INVENTORY list below.
        8. IF "app_name in target_namespace" IS in the list: Set "action": "update".
        9. IF "app_name in target_namespace" IS NOT in the list (and not a namespace action): Set "action": "create".
        10. Reasoning must explain the action and mention the namespace.
        11. CRITICAL: Never use "create" if the app already exists in that specific target namespace.
        12. CRITICAL: The field "app_name" MUST NEVER be null or missing.
        13. IF the intent is to deploy an app into a namespace that doesn't exist: The action MUST still be 'create' (for the app), and replicas MUST be 1 (or the user's requested number). 
            The framework will handle namespace creation separately.
        14. IF the user mentions 'database', 'storage', 'data', or 'volume', identify the size in GB and populate storage_gb.
        15. CONFIGURATION RULE: If the user provides file content (e.g., 'create an index.html with content <h1>Hello</h1>'), 
            you MUST populate the "config_data" field as a dictionary where the key is the filename and the value is the content string.
        16. If "config_data" is provided, the framework will automatically create a ConfigMap and mount it to /etc/config.
        17. SUMMARY RULE: Always provide a one-sentence clear explanation in the 'summary' field (e.g., 'Deploying Nginx with a custom index.html in the production namespace').
        18. STANDARD IMAGE RULE: For well-known images (e.g., nginx, redis, mysql), you MUST provide the standard container_port (e.g., 80, 6379, 3306) based on your knowledge.
        19. CUSTOM IMAGE RULE: For custom or unknown images (e.g., amityzope/my-app), if the user does not specify a port, set container_port to null. The framework will inspect it automatically.
        20. RESOURCE ESTIMATION: You MUST provide the 'resources' field with reasonable 'requests' and 'limits' for CPU and memory.
            Use your knowledge of the application type to estimate:
            - Web servers (nginx, apache): modest resources (e.g., requests.cpu ~100-500m, memory ~128Mi-512Mi)
            - Caches (redis, memcached): moderate CPU, higher memory (e.g., requests.cpu ~200-500m, memory ~256Mi-1Gi)
            - Databases (postgres, mysql, mongo): higher CPU and memory (e.g., requests.cpu ~500m-2, memory ~1Gi-4Gi)
            - App servers (nodejs, python, java): moderate resources (e.g., requests.cpu ~100-500m, memory ~128Mi-512Mi)
            The 'requests' values determine cluster capacity planning. Set 'limits' higher than requests (1.5-2x) to allow bursts.
            Consider cluster load: if cluster CPU >80% or memory >80%, be more conservative.
            DO NOT omit this field - it is required for all workload deployments.
        21. OVERALL SUMMARY RULE: You MUST provide a high-level explanation of the entire orchestration in the "overall_summary" field.
        22. STRICT FORMATTING: Output EXACTLY ONE JSON object. NEVER include text before or after the JSON.

        INFRASTRUCTURE RULES:
        23. SECRET HANDLING: If the user asks to create a secret (e.g., 'create a secret named X with key Y=Z'), you MUST:
            - Set "app_name" to the secret name.
            - Set "secret_data" as a dictionary with the provided key-value pairs.
            - Set "replicas": 0, "image": null, "service_port": null, "container_port": null.
            - Set "action": "create".
            The framework will create a Secret resource ONLY (no Deployment, no Service).
        24. INGRESS-ONLY HANDLING: If the user asks to create an Ingress for an existing service (e.g., 'create an Ingress that routes to service X'), you MUST:
            - Set "app_name" to the Ingress name.
            - Set "networking.ingress_host" to the specified host domain.
            - Set "replicas": 0, "image": null.
            - Do NOT set "config_data" or "storage_gb".
            The framework will create an Ingress resource ONLY (no Deployment, no Service). The referenced service MUST already exist.
        25. CONFIGMAP-ONLY HANDLING: If the user asks to create a ConfigMap only (e.g., 'deploy a ConfigMap named X with key Y=Z'), you MUST:
            - Set "app_name" to the ConfigMap name.
            - Set "config_data" with the provided key-value pairs.
            - Set "replicas": 0, "image": null.
            - Set "action": "create".
            The framework will create a ConfigMap resource ONLY (no Deployment, no Service).

        CLUSTER INVENTORY: {existing_apps}

        NETWORKING RULES (DO NOT MISS):
        - 'expose_internally': Set to true.
        - 'expose_externally': Set to true if it is a web server or web app or user asks for public access.
        - 'service_port': If the user mentions a specific port, YOU MUST use that exact port.
        - 'container_port': Use the native port for the image (e.g. Nginx=80, Redis=6379).

        Schema:
        {schema_def}

        FULL CONTEXT: {context}<|eot_id|>
        <|start_header_id|>user<|end_header_id|>
        {intent}<|eot_id|>
        <|start_header_id|>assistant<|end_header_id|>
        """

        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({"prompt": prompt, "temperature": 0.1, "max_gen_len": 2048})
        )
        
        # --- TOKEN EXTRACTION ---
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        usage = {
            "input_tokens": int(headers.get("x-amzn-bedrock-input-token-count", 0)),
            "output_tokens": int(headers.get("x-amzn-bedrock-output-token-count", 0)),
            "total_tokens": int(headers.get("x-amzn-bedrock-input-token-count", 0)) + \
                            int(headers.get("x-amzn-bedrock-output-token-count", 0))
        }

        raw_output = json.loads(response["body"].read())["generation"]
        
        try:
            # surgical cleaning to prevent JSON errors without changing logic
            clean_output = raw_output.strip().replace('```json', '').replace('```', '')

            # TRUNCATION CHECK: If AI stops mid-JSON, it won't end with a brace.
            if not clean_output.endswith('}'):
                raise ValueError("Plan too complex (token limit reached). Please split your request.")

            clean_json_match = re.search(r'\{.*\}', clean_output, re.DOTALL)
            
            if not clean_json_match:
                raise ValueError("No JSON found in response.")
            
            json_str = clean_json_match.group(0)

            # Surgical Fix 1: Remove trailing commas that crash json.loads
            json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
            
            # Surgical Fix 2: Flatten newlines that might break string boundaries
            json_str = json_str.replace('\n', ' ')

            plan_dict = json.loads(json_str)

            # Auto-wrap if AI misses the list structure
            if "plans" not in plan_dict:
                plan_dict = {"overall_summary": plan_dict.get("summary", "Infrastructure plan."), "plans": [plan_dict]}
            
            # Sanitization and safety
            for item in plan_dict.get("plans", []):
                raw_name = item.get("app_name") or item.get("target_namespace") or "resource"
                item["app_name"] = re.sub(r'[^a-z0-9\-]', '-', str(raw_name).lower()).strip('-')

            # Build orchestration object
            orchestration = OrchestrationPlan.model_validate(plan_dict)

            # Inject create_namespace plans for any missing namespaces
            # Collect namespaces already having a create_namespace plan
            existing_ns_plans = {p.target_namespace for p in orchestration.plans if p.action == "create_namespace"}
            new_plans = []
            namespaces_added = set()

            for p in orchestration.plans:
                # Consider workload actions that require a namespace to exist
                if p.action in ["create", "update", "delete"]:
                    ns = p.target_namespace or "default"
                    if ns not in active_namespaces and ns not in existing_ns_plans and ns not in namespaces_added:
                        # Prepend a create_namespace plan before this workload
                        create_ns = ExecutionPlan(
                            app_name=ns,
                            image=None,
                            replicas=0,
                            service_port=None,
                            container_port=None,
                            target_namespace=ns,
                            action="create_namespace",
                            networking=None,
                            autoscaling=None,
                            storage_gb=None,
                            config_data=None,
                            resources=None,
                            reasoning=f"Namespace '{ns}' does not exist, creating it first",
                            summary=f"Create namespace {ns}"
                        )
                        new_plans.append(create_ns)
                        namespaces_added.add(ns)
                new_plans.append(p)

            orchestration.plans = new_plans

            return orchestration, usage
        except Exception as e:
            # Enhanced error reporting to see the actual raw failure
            print(f"DEBUG - Raw AI Output: {raw_output}")
            raise ValueError(f"Failed to generate valid orchestration plan: {str(e)}")