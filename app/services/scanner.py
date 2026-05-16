from kubernetes import client, config
import json
import re

class ClusterScanner:
    def __init__(self):
        try:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            
            self.apps_v1 = client.AppsV1Api()
            self.core_v1 = client.CoreV1Api()
            self.custom_api = client.CustomObjectsApi()
        except Exception as e:
            print(f"K8s init warning: {e}")

    def _parse_cpu(self, cpu_str):
        if not cpu_str or cpu_str == "0": return 0
        if cpu_str.endswith('m'): return int(cpu_str[:-1])
        if cpu_str.endswith('n'): return int(int(cpu_str[:-1]) / 1000000)
        return int(float(cpu_str) * 1000)

    def _parse_memory(self, mem_str):
        if not mem_str or mem_str == "0": return 0
        units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
        match = re.match(r"(\d+)([a-zA-Z]*)", mem_str)
        if not match: return 0
        number, unit = match.groups()
        bytes_val = int(number) * units.get(unit, 1)
        return bytes_val / (1024**2)

    def get_cluster_state(self) -> str:
        try:
            # 1. Fetch Real-time Pod Metrics (actual usage)
            try:
                pod_metrics = self.custom_api.list_cluster_custom_object(
                    "metrics.k8s.io", "v1beta1", "pods"
                )
                usage_map = {}
                for item in pod_metrics.get('items', []):
                    ns = item['metadata']['namespace']
                    cpu_sum = sum(self._parse_cpu(c['usage']['cpu']) for c in item['containers'])
                    mem_sum = sum(self._parse_memory(c['usage']['memory']) for c in item['containers'])
                    usage_map[f"{ns}-{item['metadata']['name']}"] = {"cpu": cpu_sum, "mem": mem_sum}
            except Exception:
                usage_map = {}

            # 2. Fetch all pods to compute resource requests per deployment
            all_pods = self.core_v1.list_pod_for_all_namespaces(watch=False)
            deployment_pod_requests = {}  # key: "ns-deployment"
            for pod in all_pods.items:
                # Find owning deployment via ownerReferences
                owner = None
                for ref in (pod.metadata.owner_references or []):
                    if ref.kind == "ReplicaSet":
                        owner = ref.name
                        break
                if not owner:
                    continue
                # ReplicaSet name is "{deployment}-{hash}"
                dep_name = re.sub(r"-[a-z0-9]+$", "", owner)
                key = f"{pod.metadata.namespace}-{dep_name}"
                if key not in deployment_pod_requests:
                    deployment_pod_requests[key] = {"cpu": 0, "mem": 0, "pod_count": 0}
                for c in pod.spec.containers:
                    r = c.resources
                    if r and r.requests:
                        deployment_pod_requests[key]["cpu"] += self._parse_cpu(r.requests.get("cpu", "0"))
                        deployment_pod_requests[key]["mem"] += self._parse_memory(r.requests.get("memory", "0"))
                    elif r and r.limits:
                        deployment_pod_requests[key]["cpu"] += self._parse_cpu(r.limits.get("cpu", "0"))
                        deployment_pod_requests[key]["mem"] += self._parse_memory(r.limits.get("memory", "0"))
                deployment_pod_requests[key]["pod_count"] += 1

            # 3. Fetch Deployments and build workload list
            deployments = self.apps_v1.list_deployment_for_all_namespaces()
            workloads = []
            total_actual_cpu = 0
            total_actual_mem = 0

            for dep in deployments.items:
                ns = dep.metadata.namespace
                key = f"{ns}-{dep.metadata.name}"

                # Actual usage from metrics API
                pod_usage = {"cpu": 0, "mem": 0}
                for k, val in usage_map.items():
                    if k.startswith(key):
                        pod_usage = val
                        break
                total_actual_cpu += pod_usage["cpu"]
                total_actual_mem += pod_usage["mem"]

                # Requested usage from pod specs
                req = deployment_pod_requests.get(key, {"cpu": 0, "mem": 0, "pod_count": 0})
                avg_req_cpu = int(req["cpu"] / req["pod_count"]) if req["pod_count"] > 0 else 0
                avg_req_mem = int(req["mem"] / req["pod_count"]) if req["pod_count"] > 0 else 0

                workloads.append({
                    "name": dep.metadata.name,
                    "namespace": ns,
                    "replicas": dep.spec.replicas,
                    "actual_usage": {
                        "cpu": f"{int(pod_usage['cpu'])}m",
                        "memory": f"{int(pod_usage['mem'])}Mi"
                    },
                    "requested_usage": {
                        "cpu": f"{avg_req_cpu}m",
                        "memory": f"{avg_req_mem}Mi"
                    },
                    "status": "Healthy" if (dep.status.ready_replicas or 0) >= dep.spec.replicas else "Degraded"
                })

            # 4. Node Capacity for Totals (use allocatable for scheduler view)
            nodes = self.core_v1.list_node()
            total_cap_cpu = 0
            total_cap_mem = 0
            total_req_cpu = 0
            total_req_mem = 0
            for n in nodes.items:
                a = n.status.allocatable
                if a:
                    total_cap_cpu += self._parse_cpu(a.get("cpu", "0"))
                    total_cap_mem += self._parse_memory(a.get("memory", "0"))
                # Sum all pod requests on this node for global request total
            for key, req in deployment_pod_requests.items():
                total_req_cpu += req["cpu"]
                total_req_mem += req["mem"]

            state = {
                "active_namespaces": [ns.metadata.name for ns in self.core_v1.list_namespace().items],
                "existing_workloads": workloads,
                "global_metrics": {
                    "cpu_used_m": int(total_actual_cpu),
                    "cpu_cap_m": total_cap_cpu,
                    "cpu_requested_m": total_req_cpu,
                    "cpu_percent": round((total_actual_cpu / total_cap_cpu * 100), 1) if total_cap_cpu > 0 else 0,
                    "mem_used_mi": int(total_actual_mem),
                    "mem_cap_mi": int(total_cap_mem),
                    "mem_requested_mi": int(total_req_mem),
                    "mem_percent": round((total_actual_mem / total_cap_mem * 100), 1) if total_cap_mem > 0 else 0
                }
            }
            return json.dumps(state, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e), "existing_workloads": []})