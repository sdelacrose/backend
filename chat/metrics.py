import logging


logger = logging.getLogger("chat.metrics")


def emit_chat_metric(name: str, **tags):
    """
    Lightweight metrics emitter.
    Can be replaced by Prometheus/StatsD exporter later.
    """
    logger.info("chat_metric name=%s tags=%s", name, tags)
