from jinja2 import Environment, FileSystemLoader
import yaml
import base64
from kubernetes import client, config
from app.models.schemas import ExecutionPlan
from app.services.validate_resources import ResourceValidator
from app.services.inspector import ImageInspector
import time
import re
import secrets
import string
from typing import Optional, Dict

class SynthesisEngine:
    def __init__(self):
        self.env = Environment(loader=FileSystemLoader('app/templates'))
        # Add base64 encoding filter for Secrets
        self.env.filters['b64encode'] = lambda s: base64.b64encode(s.encode('utf-8')).decode('utf-8')
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
            
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.autoscaling_v2 = client.AutoscalingV2Api()
        self.networking_v1 = client.NetworkingV1Api()
        self.inspector = ImageInspector()
        
        self.validator = ResourceValidator(
            self.apps_v1, self.core_v1, self.autoscaling_v2, self.networking_v1
        )

    def check_cluster_capacity(self, plan: ExecutionPlan) -> dict:
        """Check if cluster has enough resources for this deployment"""
        try:
            nodes = self.core_v1.list_node()
            
            total_allocatable_cpu = 0  # in millicores
            total_allocatable_memory = 0  # in Mi
            total_requested_cpu = 0
            total_requested_memory = 0
            
            # Sum up allocatable resources
            for node in nodes.items:
                if node.status.allocatable:
                    cpu_str = node.status.allocatable.get('cpu', '0')
                    memory_str = node.status.allocatable.get('memory', '0Mi')
                    
                    # Parse CPU (can be "1", "100m", "0.5")
                    cpu_millis = self._parse_cpu(cpu_str)
                    total_allocatable_cpu += cpu_millis
                    
                    # Parse Memory (usually in Mi or Gi)
                    memory_mi = self._parse_memory(memory_str)
                    total_allocatable_memory += memory_mi
            
            # Get requested resources from pod spec
            if plan.resources:
                requests = plan.resources.get('requests', {})
                limits = plan.resources.get('limits', {})
                
                # Use limits if available, else requests
                req_cpu = requests.get('cpu', limits.get('cpu', '100m'))
                req_mem = requests.get('memory', limits.get('memory', '128Mi'))
                
                total_requested_cpu = self._parse_cpu(req_cpu) * (plan.replicas or 1)
                total_requested_memory = self._parse_memory(req_mem) * (plan.replicas or 1)
            
            # Check if we have enough resources
            warnings = []
            
            if total_requested_cpu > total_allocatable_cpu:
                cpu_needed = total_requested_cpu / 1000  # Convert to cores
                cpu_available = total_allocatable_cpu / 1000
                warnings.append(
                    f"⚠️  CPU Constraint: Need {cpu_needed:.1f} cores, but only {cpu_available:.1f} cores available"
                )
            
            if total_requested_memory > total_allocatable_memory:
                mem_needed = total_requested_memory / 1024  # Convert to Gi
                mem_available = total_allocatable_memory / 1024
                warnings.append(
                    f"⚠️  Memory Constraint: Need {mem_needed:.1f}Gi, but only {mem_available:.1f}Gi available"
                )
            
            return {
                "capacity_sufficient": len(warnings) == 0,
                "warnings": warnings,
                "available_cpu_cores": total_allocatable_cpu / 1000,
                "available_memory_gi": total_allocatable_memory / 1024,
                "requested_cpu_cores": total_requested_cpu / 1000,
                "requested_memory_gi": total_requested_memory / 1024
            }
        except Exception as e:
            return {
                "capacity_sufficient": True,  # If we can't check, assume OK
                "warnings": [f"Could not verify cluster capacity: {str(e)}"],
                "available_cpu_cores": 0,
                "available_memory_gi": 0,
                "requested_cpu_cores": 0,
                "requested_memory_gi": 0
            }

    def _parse_cpu(self, cpu_str: str) -> int:
        """Parse CPU string to millicores (m)"""
        if not cpu_str:
            return 0
        cpu_str = str(cpu_str).strip()
        if 'm' in cpu_str:
            return int(cpu_str.replace('m', ''))
        else:
            # Convert cores to millicores (1 = 1000m)
            return int(float(cpu_str) * 1000)

    def _parse_memory(self, mem_str: str) -> int:
        """Parse memory string to Megabytes (Mi)"""
        if not mem_str:
            return 0
        mem_str = str(mem_str).strip()
        
        if 'Gi' in mem_str:
            return int(float(mem_str.replace('Gi', '')) * 1024)
        elif 'G' in mem_str and 'Gi' not in mem_str:
            return int(float(mem_str.replace('G', '')) * 1000)
        elif 'Mi' in mem_str:
            return int(mem_str.replace('Mi', ''))
        elif 'M' in mem_str and 'Mi' not in mem_str:
            return int(float(mem_str.replace('M', '')) * 1000)
        elif 'Ki' in mem_str:
            return int(float(mem_str.replace('Ki', '')) / 1024)
        else:
            # Assume bytes
            return int(float(mem_str) / (1024 * 1024))

    def calculate_max_replicas(self, plan: ExecutionPlan) -> int:
        """Calculate maximum replicas that can fit in cluster given current capacity"""
        try:
            if not plan.resources:
                # If no resource limits specified, allow up to 10 replicas (safe default)
                return min(plan.replicas or 1, 10)
            
            nodes = self.core_v1.list_node()
            total_allocatable_cpu = 0
            total_allocatable_memory = 0
            
            # Calculate total cluster capacity
            for node in nodes.items:
                if node.status.allocatable:
                    cpu_str = node.status.allocatable.get('cpu', '0')
                    memory_str = node.status.allocatable.get('memory', '0Mi')
                    total_allocatable_cpu += self._parse_cpu(cpu_str)
                    total_allocatable_memory += self._parse_memory(memory_str)
            
            # Get per-pod resource requirements
            requests = plan.resources.get('requests', {})
            limits = plan.resources.get('limits', {})
            
            req_cpu = requests.get('cpu', limits.get('cpu', '100m'))
            req_mem = requests.get('memory', limits.get('memory', '128Mi'))
            
            cpu_per_pod = self._parse_cpu(req_cpu)
            mem_per_pod = self._parse_memory(req_mem)
            
            # Calculate max replicas based on CPU and Memory constraints
            max_replicas_cpu = total_allocatable_cpu // cpu_per_pod if cpu_per_pod > 0 else 10
            max_replicas_mem = total_allocatable_memory // mem_per_pod if mem_per_pod > 0 else 10
            
            # Take the minimum (most restrictive)
            max_replicas = min(max_replicas_cpu, max_replicas_mem)
            max_replicas = max(1, int(max_replicas))  # At least 1
            
            return max_replicas
        except Exception as e:
            print(f"[Compiler] Could not calculate max replicas: {str(e)}")
            return plan.replicas or 1  # Return requested replicas if calculation fails

    def compile_and_apply(self, plan: ExecutionPlan, namespace: str = "default",
                         dry_run: bool = False, cluster_metrics: Optional[Dict] = None) -> dict:
        # SKIP processing if this is a "no action" (already present)
        if plan.action == "no_action":
            return {
                "status": "skipped",
                "reason": "Resource already exists with same configuration",
                "manifests": {},
                "execution_logs": [f"No changes needed: {plan.app_name} is already deployed in {namespace}"]
            }
        
        execution_logs = []
        manifest_map = {}
        deep_inspection_sec = 0.0

        # Track capacity calculation details for response
        capacity_info = None

        # For update operations, fetch existing deployment image if not provided
        if plan.action == "update" and (plan.image is None or plan.image == "None" or plan.image == ""):
            try:
                existing_image = self._get_existing_deployment_image(plan.app_name, namespace)
                if existing_image:
                    plan.image = existing_image
                    execution_logs.append(f"[UPDATE] Fetched existing image: {existing_image}")
                else:
                    execution_logs.append(f"[UPDATE] Could not fetch image, using nginx fallback")
                    plan.image = "nginx"
            except Exception as e:
                execution_logs.append(f"[UPDATE] Error fetching image: {str(e)}. Using nginx fallback.")
                plan.image = "nginx"

        # Auto-generate database credentials for database images (stored in Kubernetes Secret)
        if plan.image and not plan.secret_data:
            image_lower = plan.image.lower()
            password = self._generate_random_password()
            
            if "postgres" in image_lower:
                plan.secret_data = {"POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": password}
                execution_logs.append(f"[AUTO-CONFIG] Generated PostgreSQL credentials for {plan.app_name}")
            elif "mysql" in image_lower:
                plan.secret_data = {"MYSQL_ROOT_PASSWORD": password, "MYSQL_DATABASE": "app", "MYSQL_USER": "app", "MYSQL_PASSWORD": password}
                execution_logs.append(f"[AUTO-CONFIG] Generated MySQL credentials for {plan.app_name}")
            elif "mariadb" in image_lower:
                plan.secret_data = {"MARIADB_ROOT_PASSWORD": password, "MARIADB_DATABASE": "app", "MARIADB_USER": "app", "MARIADB_PASSWORD": password}
                execution_logs.append(f"[AUTO-CONFIG] Generated MariaDB credentials for {plan.app_name}")
            elif "mongodb" in image_lower:
                plan.secret_data = {"MONGO_INITDB_ROOT_USERNAME": "admin", "MONGO_INITDB_ROOT_PASSWORD": password, "MONGO_INITDB_DATABASE": "app"}
                execution_logs.append(f"[AUTO-CONFIG] Generated MongoDB credentials for {plan.app_name}")
            elif "cassandra" in image_lower:
                plan.secret_data = {"CASSANDRA_USER": "cassandra", "CASSANDRA_PASSWORD": password}
                execution_logs.append(f"[AUTO-CONFIG] Generated Cassandra credentials for {plan.app_name}")

        # Scan image layers to estimate resources if AI didn't provide them
        if plan.image and plan.image not in ("None", "", "None") and plan.action in ("create", "update"):
            if plan.resources is None:
                estimated = self.inspector.estimate_resources_from_image(plan.image)
                if estimated:
                    plan.resources = {
                        "requests": estimated["requests"],
                        "limits": estimated["limits"]
                    }
                    execution_logs.append(
                        f"[IMAGE SCAN] Estimated resources for '{plan.image}': "
                        f"{estimated['requests']['cpu']}/{estimated['requests']['memory']} "
                        f"(category: {estimated['category']})"
                    )

        # Capacity-aware replica adjustment (only for create/update)
        if cluster_metrics and plan.action in ["create", "update"]:
            capacity_info = self._adjust_replicas_for_capacity(plan, cluster_metrics)

        # Determine if this plan involves a workload (application with container image)
        is_delete = plan.action in ["delete", "delete_namespace"]
        is_workload_action = plan.action not in ["create_namespace", "delete_namespace"]
        is_workload_resource = (plan.image is not None and plan.image != "" and plan.image != "None") or (plan.action in ["update", "delete"])
        should_render_ns = plan.action in ["create_namespace", "delete_namespace"]

        # Determine resource kind: StatefulSet for databases/stateful apps, Deployment for stateless
        is_statefulset = getattr(plan, 'resource_kind', 'deployment') == 'statefulset'

        render_queue = [
            ("Namespace", "namespace.yaml.j2", should_render_ns),
            ("ConfigMap", "configmap.yaml.j2", plan.config_data is not None or plan.action == "delete"),
            ("Secret", "secret.yaml.j2", plan.secret_data is not None or plan.action == "delete"),
            ("PersistentVolumeClaim", "pvc.yaml.j2", (plan.storage_gb is not None or plan.action == "delete") and not is_statefulset),
            ("StatefulSet", "statefulset.yaml.j2", is_workload_resource and is_statefulset),
            ("Deployment", "deployment.yaml.j2", is_workload_resource and not is_statefulset),
            ("HeadlessService", "headless-service.yaml.j2", is_workload_resource and is_statefulset and (plan.action == "delete" or plan.networking is not None)),
            ("Service", "service.yaml.j2", is_workload_resource and not is_statefulset and (plan.action == "delete" or plan.networking is not None)),
            ("Ingress", "ingress.yaml.j2", (plan.action == "delete" or (plan.networking and plan.networking.ingress_host is not None))),
            ("HorizontalPodAutoscaler", "hpa.yaml.j2", is_workload_resource and (plan.action == "delete" or plan.autoscaling is not None)),
        ]

        if is_workload_resource and not is_delete:
            if plan.image:
                insp_start = time.time()
                # 1. If the image declares ExposedPorts, trust that over AI
                try:
                    image = self.inspector._get_image(plan.image)
                    exposed_port = self.inspector._get_exposed_port(image)
                except Exception:
                    exposed_port = None
                # 2. Fall back to deep_inspect (checks Cmd/Entrypoint heuristics)
                detected_port = self.inspector.deep_inspect(plan.image)
                deep_inspection_sec = time.time() - insp_start
                if exposed_port is not None:
                    plan.container_port = exposed_port
                elif plan.container_port is None:
                    plan.container_port = detected_port
            if plan.container_port is None:
                plan.container_port = 80
            # Set service_port: use provided, or default to 80
            if plan.service_port is None:
                plan.service_port = 80

        for kind, template_name, should_run in render_queue:
            if not should_run:
                continue
            try:
                template = self.env.get_template(template_name)
                rendered_yaml = template.render(plan=plan, namespace=namespace)
                doc = yaml.safe_load(rendered_yaml)
                
                # Skip if template rendered to empty (due to conditional blocks)
                if doc is None:
                    continue
                    
                name = doc["metadata"]["name"]
                unique_key = f"{namespace}-{kind}"

                action = plan.action
                is_delete = (action == "delete")

                # For delete actions, check existence BEFORE adding to manifest map
                if is_delete:
                    exists = self._resource_exists(kind, name, namespace)
                    if not exists:
                        log_msg = f"Resource {kind}/{name} not found in namespace '{namespace}', skipping deletion"
                        execution_logs.append(log_msg)
                        continue  # Skip - do NOT add to manifest_map

                # Add to manifest map ONLY if resource exists (or is being created/updated)
                manifest_map[unique_key] = rendered_yaml

                if kind == "Namespace":
                    # Namespace is always included if should_render_ns is true (for display)
                    continue

                if dry_run:
                    if is_delete:
                        execution_logs.append(f"Dry-run: Would delete {kind}/{name}")
                    else:
                        execution_logs.append(f"Dry-run: Would {action} {kind}/{name}")
                    continue

                # Actual execution
                result = self._apply_resource(kind, name, namespace, doc, action)
                if is_delete:
                    if result:
                        execution_logs.append(f"Successfully executed {action} on {kind}: {name}")
                    else:
                        # Resource disappeared between check and deletion
                        execution_logs.append(f"Resource {kind}/{name} disappeared before deletion")
                        if unique_key in manifest_map:
                            del manifest_map[unique_key]
                else:
                    execution_logs.append(f"Successfully executed {action} on {kind}: {name}")

            except Exception as e:
                execution_logs.append(f"Error on {kind}: {str(e)}")
                print(f"[COMPILER ERROR] {kind} rendering failed for {plan.app_name} in {namespace}: {str(e)}")
                import traceback
                traceback.print_exc()

        result = {
            "status": "success",
            "namespace": namespace,
            "execution_logs": execution_logs,
            "manifests": manifest_map
        }

        # Include capacity info if calculated
        if capacity_info:
            result["capacity"] = capacity_info

        # Include internal timing for deep image inspection
        if deep_inspection_sec > 0:
            result["_debug_timing"] = {"deep_inspection_sec": deep_inspection_sec}

        return result

    def _parse_cpu(self, cpu_str):
        """Parse CPU string (e.g., '500m', '2') to millicores."""
        if not cpu_str or cpu_str == "0":
            return 0
        if isinstance(cpu_str, (int, float)):
            return int(float(cpu_str) * 1000)
        cpu_str = str(cpu_str)
        if cpu_str.endswith('m'):
            return int(cpu_str[:-1])
        if cpu_str.endswith('n'):  # nanocores to millicores
            return int(int(cpu_str[:-1]) / 1000000)
        return int(float(cpu_str) * 1000)

    def _parse_memory(self, mem_str):
        """Parse memory string (e.g., '512Mi', '2Gi') to MiB."""
        if not mem_str or mem_str == "0":
            return 0
        if isinstance(mem_str, (int, float)):
            return int(mem_str)
        mem_str = str(mem_str)
        units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "k": 1000, "M": 1000**2, "G": 1000**3}
        match = re.match(r"(\d+)([a-zA-Z]*)?", mem_str)
        if not match:
            return 0
        number, unit = match.groups()
        bytes_val = int(number) * units.get(unit, 1)
        return int(bytes_val / (1024**2))  # Convert to MiB

    def _get_actual_cluster_capacity(self) -> dict:
        """Query K8s API directly for node allocatable + sum of all pod requests.
        This is authoritative (matches what the scheduler sees), unlike the
        metrics API which reports actual CPU usage (usually much lower)."""
        try:
            nodes = self.core_v1.list_node()
            total_alloc_cpu = 0
            total_alloc_mem = 0
            for n in nodes.items:
                a = n.status.allocatable
                if a:
                    total_alloc_cpu += self._parse_cpu(a.get("cpu", "0"))
                    total_alloc_mem += self._parse_memory(a.get("memory", "0"))

            pods = self.core_v1.list_pod_for_all_namespaces(watch=False)
            total_req_cpu = 0
            total_req_mem = 0
            for pod in pods.items:
                if pod.spec.node_name:
                    for c in pod.spec.containers:
                        r = c.resources
                        if r and r.requests:
                            total_req_cpu += self._parse_cpu(r.requests.get("cpu", "0"))
                            total_req_mem += self._parse_memory(r.requests.get("memory", "0"))
                        elif r and r.limits:
                            total_req_cpu += self._parse_cpu(r.limits.get("cpu", "0"))
                            total_req_mem += self._parse_memory(r.limits.get("memory", "0"))

            return {
                "total_cpu_milli": total_alloc_cpu,
                "used_cpu_milli": total_req_cpu,
                "total_mem_mib": total_alloc_mem,
                "used_mem_mib": total_req_mem,
            }
        except Exception as e:
            print(f"[CAPACITY] Failed to query K8s API directly: {e}")
            return {}

    def _adjust_replicas_for_capacity(self, plan: ExecutionPlan, metrics: Dict) -> Optional[dict]:
        """Calculate maximum replicas that fit in available cluster capacity.
        Returns capacity_info dict for response, or None if no adjustment/calculation."""
        try:
            # Query K8s API directly — this is authoritative (request-based, not usage-based)
            actual = self._get_actual_cluster_capacity()

            gm = metrics.get("global_metrics", {})
            total_cpu_milli = actual.get("total_cpu_milli") or gm.get("cpu_cap_m", 0)
            used_cpu_milli = actual.get("used_cpu_milli") or gm.get("cpu_used_m", 0)
            total_mem_mib = actual.get("total_mem_mib") or gm.get("mem_cap_mi", 0)
            used_mem_mib = actual.get("used_mem_mib") or gm.get("mem_used_mi", 0)
        except Exception:
            return None

        # Skip capacity adjustment for delete operations or if replicas=0
        if plan.action == "delete" or plan.replicas <= 0:
            return None

        # Extract per-pod resource requirements from plan
        pod_cpu_milli = None
        pod_mem_mib = None

        if plan.resources and "requests" in plan.resources:
            # AI provided resources
            reqs = plan.resources["requests"]
            if "cpu" in reqs:
                pod_cpu_milli = self._parse_cpu(reqs["cpu"])
            if "memory" in reqs:
                pod_mem_mib = self._parse_memory(reqs["memory"])

        capacity_info = {
            "pod_requirements": {},
            "cluster_capacity": {
                "total_cpu_milli": total_cpu_milli,
                "used_cpu_milli": used_cpu_milli,
                "total_mem_mib": total_mem_mib,
                "used_mem_mib": used_mem_mib,
            },
            "replicas": {
                "requested": plan.replicas,
                "deployed": plan.replicas,  # Will update if adjusted
                "max_possible": None,
                "adjusted": False,
                "reason": None
            }
        }

        if pod_cpu_milli is None or pod_mem_mib is None:
            # AI didn't provide resources - cannot calculate capacity properly
            # Use conservative fallback based on cluster load only
            cpu_percent = (used_cpu_milli / total_cpu_milli * 100) if total_cpu_milli > 0 else 0
            mem_percent = (used_mem_mib / total_mem_mib * 100) if total_mem_mib > 0 else 0

            capacity_info["replicas"]["reason"] = "AI did not provide resource requirements"
            capacity_info["pod_requirements"] = {"cpu_milli": None, "memory_mib": None}

            if cpu_percent >= 85 or mem_percent >= 85:
                if plan.replicas > 2:
                    print(f"[CAPACITY] Cluster highly loaded ({cpu_percent:.1f}% CPU, {mem_percent:.1f}% memory). Limiting replicas from {plan.replicas} to 2")
                    plan.replicas = 2
                    capacity_info["replicas"]["deployed"] = 2
                    capacity_info["replicas"]["adjusted"] = True
                    capacity_info["replicas"]["reason"] = f"Cluster load {cpu_percent:.1f}% CPU, {mem_percent:.1f}% memory >85%"
                return capacity_info
            elif cpu_percent >= 70 or mem_percent >= 70:
                if plan.replicas > 3:
                    print(f"[CAPACITY] Cluster moderately loaded ({cpu_percent:.1f}% CPU, {mem_percent:.1f}% memory). Capping replicas at 3")
                    plan.replicas = 3
                    capacity_info["replicas"]["deployed"] = 3
                    capacity_info["replicas"]["adjusted"] = True
                    capacity_info["replicas"]["reason"] = f"Cluster load {cpu_percent:.1f}% CPU, {mem_percent:.1f}% memory >70%"
                return capacity_info
            else:
                # Cluster has capacity, no adjustment needed
                capacity_info["replicas"]["reason"] = "Cluster has capacity, no adjustment needed"
                return capacity_info

        # AI provided resources - calculate capacity precisely
        capacity_info["pod_requirements"] = {
            "cpu_milli": pod_cpu_milli,
            "memory_mib": pod_mem_mib,
            "cpu_human": f"{pod_cpu_milli}m",
            "memory_human": f"{pod_mem_mib}Mi"
        }

        # Calculate available capacity with 15% safety margin
        available_cpu_milli = (total_cpu_milli - used_cpu_milli) * 0.85
        available_mem_mib = (total_mem_mib - used_mem_mib) * 0.85

        capacity_info["available_capacity"] = {
            "cpu_milli": round(available_cpu_milli),
            "memory_mib": round(available_mem_mib),
            "safety_margin": 0.15
        }

        # Calculate maximum replicas that can fit
        max_by_cpu = int(available_cpu_milli / pod_cpu_milli) if pod_cpu_milli > 0 else float('inf')
        max_by_mem = int(available_mem_mib / pod_mem_mib) if pod_mem_mib > 0 else float('inf')

        capacity_limit = min(max_by_cpu, max_by_mem)
        capacity_info["max_by_cpu"] = max_by_cpu
        capacity_info["max_by_memory"] = max_by_mem
        capacity_info["replicas"]["max_possible"] = capacity_limit

        # Ensure at least 1 replica can fit
        if capacity_limit < 1:
            print(f"[CAPACITY] WARNING: Cluster lacks resources for even 1 replica of {plan.app_name}. Available CPU: {available_cpu_milli:.0f}m, Memory: {available_mem_mib:.0f}Mi. Needs: CPU={pod_cpu_milli}m, Memory={pod_mem_mib}Mi")
            capacity_limit = 1
            plan.replicas = 1
            capacity_info["replicas"]["deployed"] = 1
            capacity_info["replicas"]["adjusted"] = True
            capacity_info["replicas"]["reason"] = "Insufficient cluster capacity (forced 1 replica)"
            capacity_info["feasible"] = False
        else:
            capacity_info["replicas"]["reason"] = "Capacity calculation based on resource requirements"
            capacity_info["feasible"] = True

        hpa_capped = {}

        if plan.replicas > capacity_limit:
            print(f"[CAPACITY] Only space for {capacity_limit} replicas based on resource requirements. Reducing from {plan.replicas} to {capacity_limit}")
            print(f"[CAPACITY] Details: pod needs {pod_cpu_milli}m CPU, {pod_mem_mib}Mi RAM. Available: {available_cpu_milli:.0f}m CPU, {available_mem_mib:.0f}Mi RAM")
            plan.replicas = capacity_limit
            capacity_info["replicas"]["deployed"] = capacity_limit
            capacity_info["replicas"]["adjusted"] = True

            # Clamp HPA min/max to capacity limit so HPA doesn't request impossible replicas
            if plan.autoscaling and capacity_limit >= 1:
                if plan.autoscaling.max_replicas > capacity_limit:
                    old_max = plan.autoscaling.max_replicas
                    plan.autoscaling.max_replicas = capacity_limit
                    hpa_capped["max_replicas"] = {"from": old_max, "to": capacity_limit}
                    print(f"[CAPACITY] HPA max capped from {old_max} to {capacity_limit}")
                if plan.autoscaling.min_replicas > capacity_limit:
                    old_min = plan.autoscaling.min_replicas
                    plan.autoscaling.min_replicas = capacity_limit
                    hpa_capped["min_replicas"] = {"from": old_min, "to": capacity_limit}
                    print(f"[CAPACITY] HPA min capped from {old_min} to {capacity_limit}")

                if hpa_capped:
                    capacity_info["hpa_capped"] = hpa_capped
        elif capacity_info.get("feasible") is not False:
            capacity_info["replicas"]["deployed"] = plan.replicas
            capacity_info["replicas"]["reason"] = "Requested replicas fit within cluster capacity"

        return capacity_info

    def _resource_exists(self, kind: str, name: str, namespace: str) -> bool:
        """Check if a namespaced resource exists. Returns True if exists, False if 404."""
        mapping = {
            "ConfigMap": (self.core_v1, "config_map"),
            "Secret": (self.core_v1, "secret"),
            "PersistentVolumeClaim": (self.core_v1, "persistent_volume_claim"),
            "Deployment": (self.apps_v1, "deployment"),
            "StatefulSet": (self.apps_v1, "stateful_set"),
            "Service": (self.core_v1, "service"),
            "HeadlessService": (self.core_v1, "service"),
            "HorizontalPodAutoscaler": (self.autoscaling_v2, "horizontal_pod_autoscaler"),
            "Ingress": (self.networking_v1, "ingress"),
        }
        if kind not in mapping:
            raise ValueError(f"Unsupported resource kind for existence check: {kind}")
        api_client, res_type = mapping[kind]
        try:
            getattr(api_client, f"read_namespaced_{res_type}")(name=name, namespace=namespace)
            return True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return False
            else:
                raise

    def _get_existing_deployment_image(self, app_name: str, namespace: str) -> Optional[str]:
        """Fetch the image from an existing deployment. Returns image name or None if deployment not found."""
        try:
            deployment = self.apps_v1.read_namespaced_deployment(name=app_name, namespace=namespace)
            if deployment.spec.template.spec.containers:
                image = deployment.spec.template.spec.containers[0].image
                # Filter out invalid image names like "None"
                if image and image != "None" and image.strip():
                    return image
            return None
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None  # Deployment doesn't exist
            else:
                raise

    def _generate_random_password(self, length: int = 16) -> str:
        """Generate a secure random password for database credentials."""
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-"
        return ''.join(secrets.choice(alphabet) for i in range(length))

    def _apply_resource(self, kind, name, namespace, doc, action):
        mapping = {
            "ConfigMap": (self.core_v1, "config_map"),
            "Secret": (self.core_v1, "secret"),
            "PersistentVolumeClaim": (self.core_v1, "persistent_volume_claim"),
            "Deployment": (self.apps_v1, "deployment"),
            "StatefulSet": (self.apps_v1, "stateful_set"),
            "Service": (self.core_v1, "service"),
            "HeadlessService": (self.core_v1, "service"),
            "HorizontalPodAutoscaler": (self.autoscaling_v2, "horizontal_pod_autoscaler"),
            "Ingress": (self.networking_v1, "ingress"),
        }
        api_client, res_type = mapping[kind]

        if action == "delete":
            # First, check if the resource exists
            try:
                getattr(api_client, f"read_namespaced_{res_type}")(name=name, namespace=namespace)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    return False  # Resource not found
                else:
                    raise
            # Resource exists, attempt deletion
            try:
                getattr(api_client, f"delete_namespaced_{res_type}")(name=name, namespace=namespace)
                return True
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    # Resource disappeared between read and delete
                    return False
                else:
                    raise

        try:
            # Read, Patch, or Create logic
            getattr(api_client, f"read_namespaced_{res_type}")(name=name, namespace=namespace)
            getattr(api_client, f"patch_namespaced_{res_type}")(name=name, namespace=namespace, body=doc)
            return True  # Successfully patched existing resource
        except client.exceptions.ApiException as e:
            if e.status == 404:
                getattr(api_client, f"create_namespaced_{res_type}")(namespace=namespace, body=doc)
                return True  # Successfully created new resource
            else:
                raise