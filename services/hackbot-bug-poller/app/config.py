import os

from pydantic import BaseModel
from pydantic_settings import BaseSettings

# One env var per query, so a URL is pasted rather than escaped into a list.
# The <LABEL> suffix only names the query in logs.
QUERY_ENV_PREFIX = "BUGZILLA_QUERY_"


class Query(BaseModel):
    """One Bugzilla search, as pasted from the browser's address bar."""

    name: str
    url: str


def load_queries(environ: dict[str, str] | None = None) -> list[Query]:
    """Collect the BUGZILLA_QUERY_* vars, in a stable order."""
    env = os.environ if environ is None else environ
    return [
        Query(name=key[len(QUERY_ENV_PREFIX) :].lower(), url=value)
        for key, value in sorted(env.items())
        if key.startswith(QUERY_ENV_PREFIX) and value.strip()
    ]


class Settings(BaseSettings):
    hackbot_api_url: str = ""
    # From Secret Manager (`external-api-key`, shared with the UI and pulse listener).
    hackbot_api_key: str = ""
    agent_name: str = "frontend-triage"

    # Anonymous by default: with no key, Bugzilla cannot return
    # security-restricted bugs at all, which is what we want.
    bugzilla_api_url: str = "https://bugzilla.mozilla.org"
    bugzilla_api_key: str | None = None

    # Passed through to the agent; None means hackbot applies its own defaults.
    model: str | None = None
    max_turns: int | None = None
    effort: str | None = None

    poll_interval_seconds: int = 60
    # Low because failed runs count as triaged and are never retried, so a broken
    # agent Job would burn the whole query. 0 disables dispatch entirely.
    max_in_flight: int = 1
    # Completion arrives as a Pub/Sub event; a lost one would hold a slot forever.
    # Short on purpose: erring long stalls the pipeline, erring short only
    # briefly exceeds max_in_flight.
    stale_run_minutes: int = 180

    environment: str = "development"
    sentry_dsn: str | None = None

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
