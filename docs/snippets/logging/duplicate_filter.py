from logging import getLogger

from grelmicro.logging import DuplicateFilter

logger = getLogger("grelmicro.health")
logger.addFilter(DuplicateFilter())
