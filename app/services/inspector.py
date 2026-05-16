import docker
import docker.errors
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Optional, Dict

class ImageInspector:
    def __init__(self):
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception:
            self.client = None

    def _get_image(self, image_name: str):
        try:
            return self.client.images.get(image_name)
        except docker.errors.ImageNotFound:
            return self.client.images.pull(image_name)

    def validate_image_exists(self, image_name: str) -> dict:
        if not self.client:
            return {"valid": True, "message": "Docker not available — skipping image validation", "image": image_name}

        if not image_name or image_name == "None" or image_name.strip() == "":
            return {"valid": False, "message": "Image name is empty", "image": image_name}

        try:
            self.client.images.get(image_name)
            return {"valid": True, "message": f"Image '{image_name}' found locally", "image": image_name}
        except docker.errors.ImageNotFound:
            pass

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.client.images.pull, image_name)
                future.result(timeout=30)
            return {"valid": True, "message": f"Image '{image_name}' found", "image": image_name}
        except TimeoutError:
            return {"valid": True, "message": f"Image pull timed out for '{image_name}' — will attempt at deploy time", "image": image_name}
        except docker.errors.ImageNotFound:
            return {"valid": False, "message": f"Image '{image_name}' does not exist on the registry. Check the spelling or use a known image (e.g., nginx, redis, postgres).", "image": image_name}
        except docker.errors.APIError as e:
            return {"valid": False, "message": f"Docker registry error while checking '{image_name}': {str(e)}", "image": image_name}
        except Exception as e:
            return {"valid": False, "message": f"Could not validate image '{image_name}': {str(e)}", "image": image_name}

    def estimate_resources_from_image(self, image_name: str) -> Optional[Dict]:
        if not self.client:
            return None

        try:
            image = self._get_image(image_name)
        except Exception:
            return None

        config = image.attrs.get("Config", {})
        entrypoint = " ".join(config.get("Entrypoint", []) or [])
        cmd = " ".join(config.get("Cmd", []) or [])
        run_cmd = f"{entrypoint} {cmd}".lower()
        env_list = config.get("Env", [])
        env_dict = {}
        for e in env_list:
            if "=" in e:
                k, v = e.split("=", 1)
                env_dict[k.upper()] = v

        labels = {k.upper(): v for k, v in (config.get("Labels") or {}).items()}

        history = image.attrs.get("History") or []
        history_text = " ".join((h.get("created_by") or "") for h in history).lower()

        # --- CLASSIFY IMAGE ---

        # 1. Database
        if any(x in run_cmd or x in history_text for x in ["postgres", "mysqld", "mongod", "mariadbd", "cassandra", "mysqld_safe"]):
            cpu = "500m"
            mem = "1Gi"
            category = "DATABASE"
        elif any(k in env_dict for k in ["PG_VERSION", "PG_MAJOR", "MYSQL_ROOT_PASSWORD", "MYSQL_VERSION", "MONGO_VERSION", "MONGO_INITDB_ROOT_USERNAME"]):
            cpu = "500m"
            mem = "1Gi"
            category = "DATABASE"

        # 2. Java/JVM apps
        elif any(x in run_cmd or x in history_text for x in ["java", "javac", "tomcat", "jenkins", "gradle", "maven", "spring"]) or "JAVA_HOME" in env_dict or "JAVA_VERSION" in env_dict:
            cpu = "500m"
            mem = "1Gi"
            category = "JAVA"

        # 3. Message queues
        elif any(x in run_cmd or x in history_text for x in ["rabbitmq", "kafka", "zookeeper", "nats"]):
            cpu = "300m"
            mem = "512Mi"
            category = "MESSAGE_QUEUE"

        # 4. Caches
        elif any(x in run_cmd for x in ["redis-server", "memcached", "valkey"]):
            cpu = "100m"
            mem = "128Mi"
            category = "CACHE"

        # 5. Web servers
        elif any(x in run_cmd for x in ["nginx", "httpd", "apache2", "caddy", "haproxy", "envoy"]) or \
             any("nginx" in (labels.get(k, "") if isinstance(labels.get(k), str) else "") for k in labels):
            cpu = "100m"
            mem = "128Mi"
            category = "WEB_SERVER"

        # 6. Node.js
        elif any(x in run_cmd for x in ["node", "npm", "yarn", "next", "nuxt"]) or "NODE_VERSION" in env_dict:
            cpu = "200m"
            mem = "256Mi"
            category = "NODEJS"

        # 7. Python
        elif any(x in run_cmd for x in ["python", "gunicorn", "uvicorn", "flask", "django", "fastapi"]) or "PYTHON_VERSION" in env_dict:
            cpu = "200m"
            mem = "256Mi"
            category = "PYTHON"

        # 8. Go / compiled binaries
        elif any(x in run_cmd for x in ["./app", "/app", "/server", "/bin/", "/usr/local/bin/"]) and "alpine" in history_text:
            cpu = "100m"
            mem = "128Mi"
            category = "COMPILED"

        # 9. Minimal / distroless
        elif "alpine" in history_text or "alpine" in image_name.lower() or "busybox" in history_text or "scratch" in history_text or "distroless" in history_text:
            if not entrypoint and not cmd:
                cpu = "50m"
                mem = "64Mi"
                category = "MINIMAL"
            else:
                cpu = "100m"
                mem = "128Mi"
                category = "MINIMAL"

        # 10. Default fallback
        else:
            cpu = "200m"
            mem = "256Mi"
            category = "UNKNOWN"

        limits_cpu_map = {"50m": "100m", "100m": "500m", "200m": "1", "300m": "1", "500m": "1"}
        limits_mem_map = {"64Mi": "128Mi", "128Mi": "256Mi", "256Mi": "512Mi", "512Mi": "1Gi", "1Gi": "2Gi"}

        return {
            "requests": {"cpu": cpu, "memory": mem},
            "limits": {"cpu": limits_cpu_map.get(cpu, "1"), "memory": limits_mem_map.get(mem, "2Gi")},
            "category": category
        }

    def _get_exposed_port(self, image) -> Optional[int]:
        exposed = image.attrs.get("Config", {}).get("ExposedPorts", {})
        if exposed:
            return int(list(exposed.keys())[0].split('/')[0])
        return None

    def deep_inspect(self, image_name: str) -> int:
        if not self.client:
            return 80

        try:
            image = self._get_image(image_name)
            attrs = image.attrs.get("Config", {})

            port = self._get_exposed_port(image)
            if port is not None:
                return port

            run_args = " ".join(attrs.get("Cmd", []) + attrs.get("Entrypoint", []))
            port_match = re.search(r"--port\s+(\d+)", run_args)
            if port_match:
                return int(port_match.group(1))

            return 80
        except Exception:
            return 80
