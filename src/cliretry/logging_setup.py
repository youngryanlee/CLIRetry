import datetime
import json
import logging
from logging.handlers import RotatingFileHandler

from .store import private_dir


class JsonFormatter(logging.Formatter):
    def format(self, record):
        value = {"schema_version": 1,
                 "timestamp": datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc).isoformat(),
                 "level": record.levelname, "action": record.getMessage()}
        value.update(getattr(record, "metadata", {}))
        return json.dumps(value, ensure_ascii=False)


def setup_logging(config):
    directory = config.state_dir / "logs"
    private_dir(directory)
    logger = logging.getLogger("cliretry")
    logger.setLevel(config.logging.level)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(directory / "cliretry.jsonl", maxBytes=config.logging.max_bytes,
                                  backupCount=config.logging.backup_count, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger

