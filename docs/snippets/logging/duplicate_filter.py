from logging import getLogger

from grelmicro.logging import DuplicateFilter

logger = getLogger("grelmicro.health")
logger.addFilter(DuplicateFilter(allowed_repetitions=5, cache_size=100))
