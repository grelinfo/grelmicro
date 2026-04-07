from grelmicro.health import HealthRegistry

# Set a 2-second timeout per checker (default: 5s)
registry = HealthRegistry(timeout=2.0)
