import uvicorn
import sys
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Launcher")

try:
    logger.info("Importing API...")
    import api
    logger.info("API imported successfully! Starting uvicorn server on port 8000...")
    uvicorn.run(api.app, host="0.0.0.0", port=8000, log_level="info")
except Exception as e:
    logger.error(f"Failed to start engine: {e}", exc_info=True)
