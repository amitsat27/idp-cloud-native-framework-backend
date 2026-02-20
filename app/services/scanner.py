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
            # Required for fetching Metrics API data
            self.custom_api = client.CustomObjectsApi()
        except Exception as e:
            print(f"K8s init warning: {e}")

    def _parse_cpu(self, cpu_str):
        if not cpu_str or cpu_str == "0": return 0
        if cpu_str.endswith('m'): return int(cpu_str[:-1])
        if cpu_str.endswith('n'): return int(int(cpu_str[:-1]) / 1000000) # nanocores to millicores
        return int(float(cpu_str) * 1000)

    def _parse_memory(self, mem_str):
        if not mem_str or mem_str == "0": return 0
        units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
        match = re.match(r"(\d+)([a-zA-Z]*)", mem_str)
        if not match: return 0
        number, unit = match.groups()
        bytes_val = int(number) * units.get(unit, 1)
        return bytes_val / (1024**2) # Convert to MiB

    def get_cluster_state(self) -> str:
        try:
            # 1. Fetch Real-time Pod Metrics
            try:
                pod_metrics = self.custom_api.list_cluster_custom_object(
                    "metrics.k8s.io", "v1beta1", "pods"
                )
                # Map metrics by "namespace-podname" for easy lookup
                usage_map = {}
                for item in pod_metrics.get('items', []):
                    ns = item['metadata']['namespace']
                    # Metrics usually aggregate all containers in a pod
                    cpu_sum = sum(self._parse_cpu(c['usage']['cpu']) for c in item['containers'])
                    mem_sum = sum(self._parse_memory(c['usage']['memory']) for c in item['containers'])
                    usage_map[f"{ns}-{item['metadata']['name']}"] = {"cpu": cpu_sum, "mem": mem_sum}
            except Exception:
                usage_map = {}

            # 2. Fetch Deployments and map usage
            deployments = self.apps_v1.list_deployment_for_all_namespaces()
            workloads = []
            total_used_cpu = 0
            total_used_mem = 0

            for dep in deployments.items:
                ns = dep.metadata.namespace
                # Simple heuristic: find the first pod starting with the deployment name
                # In a production app, you'd use label selectors here.
                pod_usage = {"cpu": 0, "mem": 0}
                for key, val in usage_map.items():
                    if key.startswith(f"{ns}-{dep.metadata.name}"):
                        pod_usage = val
                        break
                
                total_used_cpu += pod_usage["cpu"]
                total_used_mem += pod_usage["mem"]

                workloads.append({
                    "name": dep.metadata.name,
                    "namespace": ns,
                    "replicas": dep.spec.replicas,
                    "actual_usage": {
                        "cpu": f"{int(pod_usage['cpu'])}m",
                        "memory": f"{int(pod_usage['mem'])}Mi"
                    },
                    "status": "Healthy" if (dep.status.ready_replicas or 0) >= dep.spec.replicas else "Degraded"
                })

            # 3. Node Capacity for Totals
            nodes = self.core_v1.list_node()
            total_cap_cpu = sum(self._parse_cpu(n.status.capacity.get("cpu")) for n in nodes.items)
            total_cap_mem = sum(self._parse_memory(n.status.capacity.get("memory")) for n in nodes.items)

            state = {
                "active_namespaces": [ns.metadata.name for ns in self.core_v1.list_namespace().items],
                "existing_workloads": workloads,
                "global_metrics": {
                    "cpu_used_m": int(total_used_cpu),
                    "cpu_cap_m": total_cap_cpu,
                    "cpu_percent": round((total_used_cpu / total_cap_cpu * 100), 1) if total_cap_cpu > 0 else 0,
                    "mem_used_mi": int(total_used_mem),
                    "mem_cap_mi": int(total_cap_mem),
                    "mem_percent": round((total_used_mem / total_cap_mem * 100), 1) if total_cap_mem > 0 else 0
                }
            }
            return json.dumps(state, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e), "existing_workloads": []})