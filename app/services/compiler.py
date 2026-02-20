from jinja2 import Environment, FileSystemLoader
import yaml
import base64
from kubernetes import client, config
from app.models.schemas import ExecutionPlan
from app.services.validate_resources import ResourceValidator
from app.services.inspector import ImageInspector
import time
import re
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

    def compile_and_apply(self, plan: ExecutionPlan, namespace: str = "default",
                         dry_run: bool = False, cluster_metrics: Optional[Dict] = None) -> dict:
        execution_logs = []
        manifest_map = {}

        # Track capacity calculation details for response
        capacity_info = None

        # Capacity-aware replica adjustment (only for create/update)
        if cluster_metrics and plan.action in ["create", "update"]:
            capacity_info = self._adjust_replicas_for_capacity(plan, cluster_metrics)

        # Determine if this plan involves a workload (application with container image)
        # Workloads require Deployment; infrastructure-only does not.
        is_delete = plan.action in ["delete", "delete_namespace"]
        is_workload_action = plan.action not in ["create_namespace", "delete_namespace"]
        # A workload is identified by having an image, or by update/delete actions (which target existing workloads)
        is_workload_resource = (plan.image is not None) or (plan.action in ["update", "delete"])
        should_render_ns = plan.action in ["create_namespace", "delete_namespace"]

        render_queue = [
            ("Namespace", "namespace.yaml.j2", should_render_ns),
            ("ConfigMap", "configmap.yaml.j2", plan.config_data is not None or plan.action == "delete"),
            ("Secret", "secret.yaml.j2", plan.secret_data is not None or plan.action == "delete"),
            ("PersistentVolumeClaim", "pvc.yaml.j2", plan.storage_gb is not None or plan.action == "delete"),
            ("Deployment", "deployment.yaml.j2", is_workload_resource),
            ("Service", "service.yaml.j2", is_workload_resource and (plan.action == "delete" or plan.networking is not None)),
            ("Ingress", "ingress.yaml.j2", (plan.action == "delete" or (plan.networking and plan.networking.ingress_host is not None))),
            ("HorizontalPodAutoscaler", "hpa.yaml.j2", is_workload_resource and (plan.action == "delete" or plan.autoscaling is not None)),
        ]

        if is_workload_resource and not is_delete:
            if plan.container_port is None and plan.image:
                plan.container_port = self.inspector.deep_inspect(plan.image)
            plan.container_port = plan.container_port or 80
            # Note: _arbitrate_port logic from your original file remains unchanged
            plan.service_port = 80 # Simplified for brevity, use your existing _arbitrate_port here

        for kind, template_name, should_run in render_queue:
            if not should_run:
                continue
            try:
                template = self.env.get_template(template_name)
                rendered_yaml = template.render(plan=plan, namespace=namespace)
                doc = yaml.safe_load(rendered_yaml)
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

        result = {
            "status": "success",
            "namespace": namespace,
            "execution_logs": execution_logs,
            "manifests": manifest_map
        }

        # Include capacity info if calculated
        if capacity_info:
            result["capacity"] = capacity_info

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

    def _adjust_replicas_for_capacity(self, plan: ExecutionPlan, metrics: Dict) -> Optional[dict]:
        """Calculate maximum replicas that fit in available cluster capacity.
        Returns capacity_info dict for response, or None if no adjustment/calculation."""
        try:
            gm = metrics.get("global_metrics", {})
            total_cpu_milli = gm.get("cpu_cap_m", 0)
            used_cpu_milli = gm.get("cpu_used_m", 0)
            total_mem_mib = gm.get("mem_cap_mi", 0)
            used_mem_mib = gm.get("mem_used_mi", 0)
        except Exception:
            return None  # Invalid metrics, skip adjustment

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
            capacity_info["replicas"]["reason"] = "Insufficient cluster capacity (forced 1 replica)"
        else:
            capacity_info["replicas"]["reason"] = "Capacity calculation based on resource requirements"

        if plan.replicas > capacity_limit:
            print(f"[CAPACITY] Only space for {capacity_limit} replicas based on resource requirements. Reducing from {plan.replicas} to {capacity_limit}")
            print(f"[CAPACITY] Details: pod needs {pod_cpu_milli}m CPU, {pod_mem_mib}Mi RAM. Available: {available_cpu_milli:.0f}m CPU, {available_mem_mib:.0f}Mi RAM")
            plan.replicas = capacity_limit
            capacity_info["replicas"]["deployed"] = capacity_limit
            capacity_info["replicas"]["adjusted"] = True
        else:
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
            "Service": (self.core_v1, "service"),
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

    def _apply_resource(self, kind, name, namespace, doc, action):
        mapping = {
            "ConfigMap": (self.core_v1, "config_map"),
            "Secret": (self.core_v1, "secret"),
            "PersistentVolumeClaim": (self.core_v1, "persistent_volume_claim"),
            "Deployment": (self.apps_v1, "deployment"),
            "Service": (self.core_v1, "service"),
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