"""Private operator control process: python -m crosscloud.worker (after API migration)."""
import time

from security.production_config import validate_production_config
from .dispatch import enabled, step


if __name__ == "__main__":
    validate_production_config()
    enabled()
    while True:
        if not step():
            time.sleep(1)
