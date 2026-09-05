"""Local operator-only access requires the database and encryption key ring."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doneproof.browser_artifacts import BrowserArtifacts  # noqa: E402
from doneproof.config import get_settings  # noqa: E402
from doneproof.store import Store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("purge-expired")
    export = sub.add_parser("export")
    export.add_argument("--tenant", required=True)
    export.add_argument("--artifact", required=True)
    export.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    settings = get_settings()
    store = Store(settings.storage_dsn)
    artifacts = BrowserArtifacts(store, settings)
    if args.command == "purge-expired":
        print("Expired artifacts removed:", artifacts.purge_expired())
    else:
        png = artifacts.read_for_operator(args.tenant, args.artifact)
        if png is None:
            parser.exit(1, "Artifact not found or retention expired.\n")
        with args.output.open("xb") as output:
            output.write(png)
        store.audit(args.tenant, "browser.artifact_exported", "browser_artifact", args.artifact, {})
        print("Screenshot exported.")


if __name__ == "__main__":
    main()
