import json
import re
from typing import Tuple, Dict, Optional
from app.core.client import client
from app.core.config import settings
from app.models.schemas import OrchestrationPlan, ExecutionPlan

class OrchestrationPlanner:
    def __init__(self):
        self.client = client.get_bedrock_client()
        self.model_id = settings.BEDROCK_MODEL_ID

    def _is_ambiguous_intent(self, intent: str) -> bool:
        """Check if the intent is too ambiguous to process clearly."""
        intent_lower = intent.lower().strip()
        
        # Check if intent is too short
        if len(intent_lower) < 5:
            return True
        
        # Check if intent has clear action keyword
        action_keywords = ['deploy', 'create', 'delete', 'scale', 'update', 'setup', 'install', 'remove']
        has_action = any(keyword in intent_lower for keyword in action_keywords)
        
        if not has_action:
            return True
        
        # Check if intent has an app name/resource after action keyword
        words = intent_lower.split()
        has_resource = False
        
        for i, word in enumerate(words):
            if word in action_keywords and i + 1 < len(words):
                next_word = words[i + 1]
                # Check if next word looks like an app name (not an article)
                if len(next_word) > 1 and next_word not in ['the', 'my', 'a', 'an', 'it', 'this', 'that', 'something', 'stuff', 'things']:
                    has_resource = True
                    break
        
        # If we have action but no resource, it's ambiguous
        if not has_resource:
            return True
        
        # Check for scale without a number
        if 'scale' in intent_lower:
            has_number = any(char.isdigit() for char in intent_lower)
            if not has_number:
                return True
        
        # Otherwise, it's clear
        return False

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
              "resource_kind": "deployment" | "statefulset",
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
        # Check for ambiguous intent first
        if self._is_ambiguous_intent(intent):
            # Return a special ambiguous response
            ambiguous_plan = ExecutionPlan(
                app_name="ambiguous_intent",
                image=None,
                replicas=0,
                service_port=None,
                container_port=None,
                target_namespace="default",
                action="ambiguous",
                networking=None,
                autoscaling=None,
                storage_gb=None,
                config_data=None,
                secret_data=None,
                resources=None,
                reasoning="Your intent is too vague or ambiguous to process. Please provide more specific details.",
                summary="Ambiguous intent - cannot proceed"
            )
            
            orchestration = OrchestrationPlan(
                overall_summary="Unable to interpret your request - please provide more details",
                plans=[ambiguous_plan]
            )
            
            return orchestration, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        
        try:
            state_data = json.loads(context)
            existing_workloads = state_data.get('existing_workloads', [])
            active_namespaces = state_data.get('active_namespaces', [])
        except Exception:
            existing_workloads = []
            active_namespaces = []
        
        # Check for duplicate: app with same replicas in same namespace
        duplicate_check = self._check_if_duplicate_exists(intent, existing_workloads)
        if duplicate_check:
            return duplicate_check
        schema_def = self._get_compact_schema()
        # PROMPT: Original Rules 1-19 kept. Added Rule 20 and Rule 21.
        prompt = f"""<|begin_of_text|><|start_header_id|>system<|end_header_id|>
        Role: Expert K8s Orchestration Planner
        Task: Map INTENT to JSON based on current cluster STATE.

        STRICT MULTI-RESOURCE RULE:
        1. If the intent involves multiple resources (e.g., 'create namespaces prod and dev'), you MUST provide a separate entry in the "plans" list for EACH resource. Output ONE JSON object matching the OrchestrationPlan schema.

        GENERIC INTENT INTERPRETATION RULE (APPLIES TO ALL):
        0. Use your expertise and natural language understanding to analyze and interpret the user's intent. If the intent doesn't perfectly match specific rules below, use your best judgment to map it to the appropriate action and parameters. The rules below provide guidance, not absolute constraints.

        STRICT ACTION RULES:
        1. If the intent is to scale or set the number of replicas for an app (e.g., 'scale nginx to 2 replicas', 'set nginx replicas to 3'), ALWAYS set "action": "update" and set "replicas" to the requested total, regardless of the current count. This applies to any app and any namespace.
        1a. RANGE RULE: If the user specifies a range of replicas (e.g., '3-7 replicas', '2 to 5 replicas', 'min 2 max 5'), set "replicas" to the MINIMUM value of that range. The maximum goes into autoscaling.max_replicas. Example: "3-7 replicas" → replicas: 3, autoscaling: {{ "min_replicas": 3, "max_replicas": 7 }}.
        2. If number of replicas is not present, make number of replicas as 1 default.
        3. Identify the 'target_namespace' from user intent (e.g., 'staging', 'production').
        4. IF no namespace is mentioned, YOU MUST set "target_namespace": "default". 
        5. IF the intent is ONLY to create/delete a namespace (e.g., 'create namespace staging'): Use action 'create_namespace', replicas: 0, and image: null.
        6. IF the intent is to delete an app: Set "action": "delete".
        7. Identify the 'app_name' from user intent (e.g., 'nginx-web-server').
         8. Check if the exact 'app_name' exists IN the 'target_namespace' using the CLUSTER INVENTORY list below. If it exists in that namespace, use "no_action" (regardless of replica count — the system handles detection automatically).
        9. IF "app_name in target_namespace" IS in the list: Set "action": "update".
        10. IF "app_name in target_namespace" IS NOT in the list (and not a namespace action): Set "action": "create".
        11. "reasoning": "Explain the classification (e.g., 'Classified as STANDALONE_BASE, skipping service creation and the namespace used also add some info about it.) in SUMMARY AS WELL"
        12. CRITICAL: Never use "create" if the app already exists in that specific target namespace.
        13. CRITICAL: The field "app_name" MUST NEVER be null or missing.
        14. IF the intent is to deploy an app into a namespace that doesn't exist: The action MUST still be 'create' (for the app), and replicas MUST be 1 (or the user's requested number). 
            The framework will handle namespace creation separately.
        15. IF the user mentions 'database', 'storage', 'data', or 'volume', identify the size in GB and populate storage_gb.
        16. CONFIGURATION RULE: If the user provides file content (e.g., 'create an index.html with content <h1>Hello</h1>'), 
            you MUST populate the "config_data" field as a dictionary where the key is the filename and the value is the content string.
        17. If "config_data" is provided, the framework will automatically create a ConfigMap and mount it to /etc/config.
        18. SUMMARY RULE: Provide a one-sentence clear explanation in the 'summary' field.
        19. STANDARD IMAGE RULE: For well-known images (e.g., nginx, redis, mysql), you MUST provide the standard container_port (e.g., 80, 6379, 3306) based on your knowledge, if image is based image dont provide any port.
        20. CUSTOM IMAGE RULE: For custom or unknown images (e.g., amityzope/my-app), if the user does not specify a port, set container_port to null. The framework will inspect it automatically.
        21. RESOURCE ESTIMATION: You MUST provide the 'resources' field based on the image type. Use your knowledge to estimate CPU/memory:
            - Web Servers (nginx, apache, httpd): requests: cpu: "100m", memory: "128Mi", limits: cpu: "500m", memory: "512Mi"
            - Lightweight Apps (node, python, flask): requests: cpu: "100m", memory: "128Mi", limits: cpu: "500m", memory: "512Mi"
            - Heavy App Servers (tomcat, java): requests: cpu: "200m", memory: "256Mi", limits: cpu: "1", memory: "1Gi"
            - Databases (mysql, postgres, mongodb): requests: cpu: "250m", memory: "256Mi", limits: cpu: "1", memory: "2Gi"
            - Caches (redis, memcached): requests: cpu: "100m", memory: "128Mi", limits: cpu: "500m", memory: "1Gi"
            - Message Queues (rabbitmq, kafka): requests: cpu: "200m", memory: "256Mi", limits: cpu: "1", memory: "2Gi"
            - If user specifies resources in intent, use those instead. Otherwise, pick the best fit based on image characteristics from your knowledge.
        22. BASE IMAGE RULE: For utility images like alpine or busybox, do not assume a service is needed. Set service_port: null and networking: null unless the user explicitly asks to "expose" it.
        23. IMAGE CATEGORIZATION RULE: Before generating the plan, classify the image into one of these categories:

            WEB_SERVER: (e.g., nginx, httpd, apache) -> Action: Create Deployment + Service; set expose_externally: true.

            DATABASE: (e.g., postgres, mysql, mongodb) -> Action: Create Deployment (or StatefulSet) + Service; set storage_gb to at least 10 if not specified.

            STANDALONE_BASE: (e.g., alpine, busybox, ubuntu) -> Action: Create Deployment ONLY; set networking: null and replicas: 1.

            APP_RUNTIME: (e.g., node, python, openjdk) -> Action: Create Deployment + Service (Internal only) unless a port is specified.

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
        26. MINIMAL DEPLOYMENT RULE: If the intent uses the word 'deploy' without mentioning 'service', 'access', 'port', or 'expose' (e.g., 'deploy alpine'), you MUST set networking: null. The framework will then only create the Deployment and skip the Service resource.
        27. STATEFULSET RULE: If the application is a DATABASE (postgres, mysql, mongodb, mariadb, cassandra, redis, elasticsearch, rabbitmq, kafka, zookeeper, minio) OR if the user requests persistent storage (storage_gb is set), you MUST set "resource_kind": "statefulset". For all other applications (web servers, app runtimes, utilities), set "resource_kind": "deployment". StatefulSets provide stable pod identities and automatic per-pod PVCs via volumeClaimTemplates.

        DUPLICATE CHECK RULE - MANDATORY:
        Before creating or updating ANY app, ALWAYS check the inventory JSON above.
        - Find the entry where name matches the user's app AND namespace matches (or default if not specified)
        - Extract the replicas field from that entry
        - Compare with user's requested replicas
        - If found → action: "no_action" (replica count handled automatically)
        - If found with DIFFERENT replicas → action: "update"
        - If NOT found in inventory → action: "create"

        NETWORKING RULES (DO NOT MISS):
        - 'expose_internally': Set to true.
        - 'expose_externally': Set to true if it is a web server or web app or user asks for public access.
        - 'container_port': Use the native port for the image (e.g. Nginx=80, Redis=6379).
        - 'networking': If category is STANDALONE_BASE, set to null.
        - 'service_port': If category is WEB_SERVER and no port is mentioned, default to 80. If the user mentions a specific port, YOU MUST use that exact port. If DATABASE, default to the standard port (e.g., 5432 for Postgres).

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
            
            # Ensure overall_summary exists
            if "overall_summary" not in plan_dict:
                plan_dict["overall_summary"] = f"Infrastructure plan with {len(plan_dict.get('plans', []))} action(s)."
            
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

    def _check_if_duplicate_exists(self, intent: str, existing_workloads: list) -> Optional[Tuple[OrchestrationPlan, Dict]]:
        """Check if app exists with same replicas in namespace. Returns OrchestrationPlan if duplicate found, else None."""
        # Extract app name from intent (word after deploy/scale/create)
        words = intent.lower().split()
        app_name = None
        requested_replicas = 1
        target_namespace = "default"
        
        for i, word in enumerate(words):
            if word in ["deploy", "scale", "create"]:
                if i + 1 < len(words):
                    app_name = words[i + 1]
                break
        
        if not app_name:
            return None
        
        # Clean app name (only alphanumeric and hyphens; replace slashes with hyphens)
        app_name = re.sub(r'[^a-z0-9\-]', '', app_name.replace('/', '-')).lower()
        
        # Extract replica count if specified
        replica_match = re.search(r'(?:with|to)\s+(\d+)\s+replicas?', intent.lower())
        if replica_match:
            requested_replicas = int(replica_match.group(1))
        
        # Extract namespace if specified
        namespace_match = re.search(r'(?:in|from)\s+([a-z0-9\-]+)\s+namespace', intent.lower())
        if namespace_match:
            target_namespace = namespace_match.group(1)
        
        # Check if app exists with same replicas in same namespace
        for workload in existing_workloads:
            if workload.get("name") == app_name and workload.get("namespace") == target_namespace:
                
                # Found duplicate - return no_action plan
                plan = ExecutionPlan(
                    app_name=app_name,
                    image=workload.get("image"),
                    replicas=workload.get("replicas", requested_replicas),
                    service_port=None,
                    container_port=None,
                    target_namespace=target_namespace,
                    action="no_action",
                    networking=None,
                    autoscaling=None,
                    storage_gb=None,
                    config_data=None,
                    secret_data=None,
                    resources=None,
                    reasoning=f"{app_name} already exists in {target_namespace} namespace with {workload.get('replicas', requested_replicas)} replicas",
                    summary=f"{app_name.capitalize()} already exists in {target_namespace} with {workload.get('replicas', requested_replicas)} replicas"
                )
                
                orchestration = OrchestrationPlan(
                    overall_summary=f"{app_name.capitalize()} already deployed - no changes needed",
                    plans=[plan]
                )
                
                return orchestration, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        
        return None