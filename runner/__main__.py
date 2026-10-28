"""`python -m runner [--once] [--fake]`: run the poll loop (docs/RUNNER.md)."""
import argparse
import sys

from .loop import Log, Runner, Settings, make_workspace


def main(argv=None):
    parser = argparse.ArgumentParser(prog="runner", description=__doc__)
    parser.add_argument("--once", action="store_true", help="handle at most one job, then exit (0 done, 3 idle, 1 failed)")
    parser.add_argument("--fake", action="store_true", help="use the in-memory FakeWorkspace (smoke test; no network)")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        if not args.fake and (not settings.workspace_url or not settings.runner_token):
            parser.error("WORKSPACE_URL and RUNNER_TOKEN are required (or pass --fake)")
        if settings.privacy_tier == "strict_local" and settings.model_base_url:
            from campaign_tool.digest.model import ModelError, check_config
            try:
                check_config(settings.model_config())
            except ModelError as exc:
                parser.error(str(exc))
    except ValueError as exc:
        parser.error(str(exc))
    runner = Runner(make_workspace(settings, fake=args.fake), settings, log=Log())
    runner.install_signals()
    return runner.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
