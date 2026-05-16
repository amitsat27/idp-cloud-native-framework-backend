import time
from kubernetes import client

class ResourceValidator:
    def __init__(self, apps_v1, core_v1, autoscaling_v2, networking_v1):
        self.apps_v1 = apps_v1
        self.core_v1 = core_v1
        self.autoscaling_v2 = autoscaling_v2
        self.networking_v1 = networking_v1
        self.timeout = 180  # Max seconds to wait (3 minutes for slow containers like Tomcat)
        self.image_pull_timeout = 300  # Max seconds to wait for image pull (5 minutes)
        self.pending_timeout = 30  # Max seconds to wait for pending pods before checking events

    def wait_for_image_pull(self, resources, namespace):
        """
        Waits for all images associated with resources to be pulled.
        :param resources: List of dicts like [{"kind": "Deployment", "name": "nginx"}]
        :param namespace: Namespace to check in
        :return: True if all images pulled, False if timeout
        """
        print(f"[Image Pull] Waiting for images to be pulled in {namespace}...")
        start_time = time.time()
        
        while time.time() - start_time < self.image_pull_timeout:
            try:
                all_images_pulled = True
                
                for res in resources:
                    kind = res["kind"]
                    name = res["name"]
                    
                    if kind in ("Deployment", "StatefulSet"):
                        if kind == "Deployment":
                            dep = self.apps_v1.read_namespaced_deployment(name, namespace)
                        else:
                            dep = self.apps_v1.read_namespaced_stateful_set(name, namespace)
                        selector = dep.spec.selector.match_labels
                        label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                        pods = self.core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                        
                        if not pods.items:
                            # Pods not created yet
                            all_images_pulled = False
                            break
                        
                        for pod in pods.items:
                            if pod.status.container_statuses is None:
                                all_images_pulled = False
                                break
                            
                            for container_status in pod.status.container_statuses:
                                image_id = container_status.image_id
                                # image_id is empty until image is pulled
                                if not image_id:
                                    all_images_pulled = False
                                    print(f"[Image Pull] Pod {pod.metadata.name}: Image not pulled yet for {container_status.name}")
                                    break
                            
                            if not all_images_pulled:
                                break
                    
                    if not all_images_pulled:
                        break
                
                if all_images_pulled:
                    print(f"[Image Pull] All images pulled in {namespace}")
                    return True
                
            except Exception as e:
                print(f"[Image Pull] Error checking image pull status: {str(e)}")
                pass
            
            time.sleep(2)
        
        print(f"[Image Pull] WARNING: Image pull timeout reached after {self.image_pull_timeout}s")
        return False

    def _check_pod_failure_reason(self, pod, namespace):
        """
        Check if a pod has events explaining why it's stuck (e.g., Pending, ImagePullBackOff)
        Returns: (is_failed, reason_message)
        """
        try:
            events = self.core_v1.list_namespaced_event(namespace)
            for event in events.items:
                if event.involved_object.name == pod.metadata.name:
                    if event.reason in ["Unschedulable", "FailedScheduling", "Insufficient", "OutOfMemory", "OutOfCpu"]:
                        reason = event.message or event.reason
                        return (True, reason)
                    elif event.reason == "BackOff" and "ImagePullBackOff" in str(pod.status.container_statuses[0].state if pod.status.container_statuses else ""):
                        return (True, "Image pull failed or image not found")
            return (False, None)
        except Exception as e:
            print(f"[Validation] Could not check pod events: {str(e)}")
            return (False, None)

    def verify_readiness(self, resources, namespace):
        """
        Polls Kubernetes until all resources reach their expected state.
        :param resources: List of dicts like [{"kind": "Deployment", "name": "nginx"}]
        """
        results = []

        for res in resources:
            kind = res["kind"]
            name = res["name"]
            status = "Pending"
            pod_statuses = []
            print(f"Checking state for {kind}/{name} in {namespace}...")
            start_time = time.time()

            while time.time() - start_time < self.timeout:
                try:
                    if kind == "Deployment":
                        readiness = self.apps_v1.read_namespaced_deployment_status(name, namespace)
                        if (readiness.status.available_replicas is not None and
                            readiness.status.available_replicas >= readiness.spec.replicas):
                            selector = readiness.spec.selector.match_labels
                            label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                            pods = self.core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                            all_running = True
                            pod_statuses = []
                            for pod in pods.items:
                                phase = pod.status.phase
                                pod_statuses.append({"pod": pod.metadata.name, "phase": phase})
                                if phase != "Running":
                                    all_running = False
                            if all_running and pods.items:
                                status = "Ready"
                                break
                    elif kind == "StatefulSet":
                        sts = self.apps_v1.read_namespaced_stateful_set_status(name, namespace)
                        if (sts.status.ready_replicas is not None and
                            sts.status.ready_replicas >= sts.spec.replicas):
                            selector = sts.spec.selector.match_labels
                            label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                            pods = self.core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                            all_running = True
                            pod_statuses = []
                            for pod in pods.items:
                                phase = pod.status.phase
                                pod_statuses.append({"pod": pod.metadata.name, "phase": phase})
                                if phase != "Running":
                                    all_running = False
                            if all_running and pods.items:
                                status = "Ready"
                                break

                    # CHECK FOR STUCK PENDING PODS (fail faster)
                    elif time.time() - start_time > self.pending_timeout and kind == "Deployment":
                        selector = readiness.spec.selector.match_labels
                        label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                        pods = self.core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                        pod_statuses = []
                        has_pending = False
                        for pod in pods.items:
                            phase = pod.status.phase
                            pod_statuses.append({"pod": pod.metadata.name, "phase": phase})
                            if phase == "Pending":
                                has_pending = True
                                is_failed, reason = self._check_pod_failure_reason(pod, namespace)
                                if is_failed:
                                    status = "FailedScheduling"
                                    print(f"[Validation] Pod {pod.metadata.name} failed: {reason}")
                                    break

                        if status == "FailedScheduling":
                            break
                        elif has_pending:
                            print(f"[Validation] WARNING: Pods for {name} still Pending after {self.pending_timeout}s - checking events...")
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
                        if pvc.status.phase == "Bound":
                            status = "Ready"
                            break
                    elif kind == "ConfigMap":
                        self.core_v1.read_namespaced_config_map(name, namespace)
                        status = "Ready"
                        break
                except client.exceptions.ApiException as e:
                    if e.status == 404:
                        pass
                except Exception:
                    pass
                time.sleep(2)

            # After timeout, if not ready, check pod statuses for Deployments/StatefulSets
            if kind in ("Deployment", "StatefulSet") and status != "Ready":
                try:
                    if kind == "Deployment":
                        dep = self.apps_v1.read_namespaced_deployment_status(name, namespace)
                    else:
                        dep = self.apps_v1.read_namespaced_stateful_set_status(name, namespace)
                    selector = dep.spec.selector.match_labels
                    label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                    pods = self.core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                    pod_statuses = []
                    failure_reasons = []
                    for pod in pods.items:
                        phase = pod.status.phase
                        pod_statuses.append({"pod": pod.metadata.name, "phase": phase})
                        if phase in ["Pending", "Failed", "ImagePullBackOff"]:
                            is_failed, reason = self._check_pod_failure_reason(pod, namespace)
                            if reason:
                                failure_reasons.append(f"{pod.metadata.name}: {reason}")
                    if pods.items:
                        status = "NotReady"
                        if failure_reasons:
                            status = "FailedScheduling"
                except Exception:
                    pass

            result = {"resource": f"{kind}/{name}", "status": status}
            if pod_statuses:
                result["pod_statuses"] = pod_statuses
            if 'failure_reasons' in locals() and failure_reasons:
                result["failure_reasons"] = failure_reasons
            results.append(result)
        return results