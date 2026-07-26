import logging
import signal
import sys
import threading

from app import bugzilla, poller
from app.bugzilla import QueryConfigError
from app.config import QUERY_ENV_PREFIX, Query, load_queries, settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_stop = threading.Event()


def validate(queries: list[Query]) -> None:
    """Fail fast on configuration, but ride out a Bugzilla wobble.

    A malformed URL will never fix itself, so it exits non-zero and the bad
    deploy is obvious. A Bugzilla error might just be Bugzilla, so it is logged
    and the next tick retries. Each query's hit count is logged here so a
    mistyped search is visible at startup -- compare it against what Bugzilla
    showed you when you copied the URL.
    """
    for query in queries:
        try:
            params = bugzilla.to_rest_params(query.url)
        except QueryConfigError as exc:
            logger.error(
                "%s%s is not usable: %s", QUERY_ENV_PREFIX, query.name.upper(), exc
            )
            sys.exit(1)

        logger.info("query %s -> %s", query.name, params)
        try:
            bugzilla.search(query)
        except Exception:
            logger.exception(
                "query %s could not be run at startup; will retry next tick", query.name
            )


def main() -> None:
    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment)

    if not (settings.hackbot_api_url and settings.hackbot_api_key):
        logger.error("HACKBOT_API_URL/HACKBOT_API_KEY are not set; refusing to start")
        sys.exit(1)

    queries = load_queries()
    if not queries:
        logger.error(
            "no queries configured; set at least one %s<LABEL> to a Bugzilla "
            "buglist.cgi URL",
            QUERY_ENV_PREFIX,
        )
        sys.exit(1)

    validate(queries)

    def shutdown(signum, _frame):
        logger.info("Received signal %s; shutting down", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info(
        "Polling %d Bugzilla quer%s every %ds; up to %d %s runs in flight",
        len(queries),
        "y" if len(queries) == 1 else "ies",
        settings.poll_interval_seconds,
        settings.max_in_flight,
        settings.agent_name,
    )
    while not _stop.is_set():
        try:
            poller.tick(queries)
        except Exception:
            # A failed tick must never kill the loop; the next one retries.
            logger.exception("tick failed")
        _stop.wait(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
