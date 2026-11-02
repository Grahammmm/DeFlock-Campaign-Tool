"""`python -m runner [--once] [--fake]`: run the poll loop (docs/RUNNER.md)."""
import argparse
import json
import sys

from .loop import Log, Runner, Settings, make_workspace


def main(argv=None):
    parser = argparse.ArgumentParser(prog="runner", description=__doc__)
    parser.add_argument("--once", action="store_true", help="handle at most one job, then exit (0 done, 3 idle, 1 failed)")
    parser.add_argument("--fake", action="store_true", help="use the in-memory FakeWorkspace (smoke test; no network)")
    commands = parser.add_subparsers(dest="command")
    export_parser = commands.add_parser("export-hosted", help="write a verified private archive using the runner environment")
    export_parser.add_argument("--out", required=True, help="new archive path (never overwritten)")
    args = parser.parse_args(argv)
    if args.command == "export-hosted":
        if args.fake or args.once:
            parser.error("export-hosted cannot be combined with --fake or --once")
        return export_hosted_command(args.out, parser)
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


def export_hosted_command(out_path, parser):
    from campaign_tool.backup import export_hosted

    try:
        settings = Settings.from_env()
        if not settings.workspace_url or not settings.runner_token:
            parser.error("WORKSPACE_URL and RUNNER_TOKEN are required")
        manifest = export_hosted(make_workspace(settings), out_path)
    except Exception as exc:
        # Remote error bodies and transport exceptions are untrusted and may
        # echo credentials. Never print them (or the environment) here.
        print(f"Stopped: hosted export failed ({type(exc).__name__}); no completed archive written", file=sys.stderr)
        return 1
    print(json.dumps({"manifest_sha256": manifest["manifest_sha256"], "objects": manifest["counts"]["objects"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
