import docker
import re

class ImageInspector:
    def __init__(self):
        try:
            # Verified working with urllib3<2.0
            self.client = docker.from_env()
        except Exception:
            self.client = None

    def deep_inspect(self, image_name: str) -> int:
        if not self.client:
            return 80
            
        try:
            # Pull metadata (fast if image is local)
            image = self.client.images.pull(image_name)
            
            attrs = image.attrs.get("Config", {})
            
            # 1. Check EXPOSE
            exposed = attrs.get("ExposedPorts", {})
            if exposed:
                return int(list(exposed.keys())[0].split('/')[0])

            # 2. Check CMD/Entrypoint (Crucial for your Uvicorn app)
            run_args = " ".join(attrs.get("Cmd", []) + attrs.get("Entrypoint", []))
            port_match = re.search(r"--port\s+(\d+)", run_args)
            if port_match:
                return int(port_match.group(1))

            return 80 # Default fallback
        except Exception:
            return 80