from fastapi import APIRouter, HTTPException
from app.services.scanner import ClusterScanner
import json

router = APIRouter()
scanner = ClusterScanner()

@router.get("/state")
def get_current_cluster_state(namespace: str = "default"):
    """
    Fetches the real-time state of workloads directly from the Kubernetes API.
    """
    try:
        # The scanner returns a JSON-formatted string, 
        # so we parse it back to a Python dict for a proper FastAPI JSON response.
        raw_state = scanner.get_cluster_state()
        parsed_state = json.loads(raw_state)
        
        # If the scanner caught an error (like K8s being down), raise a 500
        if "error" in parsed_state:
            raise HTTPException(status_code=500, detail=parsed_state["error"])
            
        return {
            "status": "success",
            "namespace": namespace,
            "data": parsed_state
        }
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse cluster state data.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))