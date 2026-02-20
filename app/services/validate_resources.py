import time
from kubernetes import client

class ResourceValidator:
    def __init__(self, apps_v1, core_v1, autoscaling_v2, networking_v1):
        self.apps_v1 = apps_v1
        self.core_v1 = core_v1
        self.autoscaling_v2 = autoscaling_v2
        self.networking_v1 = networking_v1
        self.timeout = 60  # Max seconds to wait

    def verify_readiness(self, resources, namespace):
        """
        Polls Kubernetes until all resources reach their expected state.
        :param resources: List of dicts like [{"kind": "Deployment", "name": "nginx"}]
        """
        results = []
        start_time = time.time()

        for res in resources:
            kind = res["kind"]
            name = res["name"]
            status = "Pending"
            
            print(f"Checking state for {kind}/{name} in {namespace}...")

            while time.time() - start_time < self.timeout:
                try:
                    if kind == "Deployment":
                        readiness = self.apps_v1.read_namespaced_deployment_status(name, namespace)
                        if (readiness.status.available_replicas is not None and 
                            readiness.status.available_replicas >= readiness.spec.replicas):
                            status = "Ready"
                            break
                    
                    elif kind == "Service":
                        self.core_v1.read_namespaced_service(name, namespace)
                        status = "Ready"
                        break
                    
                    elif kind == "HorizontalPodAutoscaler":
                        self.autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name, namespace)
                        status = "Ready"
                        break

                    elif kind == "Ingress":
                        self.networking_v1.read_namespaced_ingress(name, namespace)
                        status = "Ready"
                        break

                    elif kind == "PersistentVolumeClaim":
                        pvc = self.core_v1.read_namespaced_persistent_volume_claim(name, namespace)
                        # A PVC is only 'Ready' when its phase is 'Bound'
                        if pvc.status.phase == "Bound":
                            status = "Ready"
                            break
                    
                    elif kind == "ConfigMap":
                        self.core_v1.read_namespaced_config_map(name, namespace)
                        status = "Ready"
                        break

                except client.exceptions.ApiException as e:
                    # Logic Change: If we are verifying a DELETE intent, 
                    # a 404 error means the resource is successfully gone.
                    # Note: This version assumes the validator is called after the action. 
                    # To keep it simple, we just pass on 404s during polling.
                    if e.status == 404:
                        pass
                except Exception:
                    pass
                
                time.sleep(2) 
            
            results.append({"resource": f"{kind}/{name}", "status": status})
        
        return results