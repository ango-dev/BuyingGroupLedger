"""Print the container's scheduling knobs as shell assignments, resolved from config.json.

`docker/entrypoint.sh` sources this instead of reading the environment itself:

    python -m scripts.container_settings > /tmp/container.env && . /tmp/container.env

WHY THIS EXISTS. docker-compose interpolates `${RUN_INTERVAL_HOURS:-3}` when it parses the compose
file — long before any Python runs — so the config file could never reach these four values without
something reading it on the container's behalf. That would have left the schedule, the timezone and
the two start-up switches as the one corner of the setup you still had to configure somewhere else.

The values go through `config.settings`, so they follow the SAME order as everything else:
environment first, then config.json, then the default. A variable already exported (by compose, or by
`docker run -e`) therefore still wins — this only fills in what the environment did not say.

Output is `export NAME='value'` — exported, not merely set, because TZ has to reach supercronic and
every scrape it spawns. Single-quoted with embedded quotes escaped, so a timezone or any future
string value cannot break the shell that sources it.
"""

from config.settings import settings

#: The shell name each setting is exported as, and where its value comes from. Named for the
#: environment variables the entrypoint and docker-compose.yml already document, so nothing that
#: reads them has to change.
EXPORTS = (
    ("RUN_INTERVAL_HOURS", lambda: str(settings.container_run_interval_hours)),
    ("RUN_ON_START", lambda: "true" if settings.container_run_on_start else "false"),
    ("PREFLIGHT_STRICT", lambda: "true" if settings.container_preflight_strict else "false"),
    ("TZ", lambda: settings.container_timezone),
    # Whether entrypoint.sh starts the read-only web dashboard beside the scheduler, and whether
    # healthcheck.sh probes it. Same container, same config file.
    ("WEB_ENABLED", lambda: "true" if settings.web_enabled else "false"),
)


def _quote(value: str) -> str:
    """Single-quote for POSIX sh, escaping any embedded single quote."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def render() -> str:
    return "\n".join(f"export {name}={_quote(read())}" for name, read in EXPORTS)


if __name__ == "__main__":
    print(render())
