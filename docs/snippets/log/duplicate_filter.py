from logging import getLogger

from grelmicro.log import DuplicateFilter

logger = getLogger("grelmicro.health")
logger.addFilter(DuplicateFilter())
