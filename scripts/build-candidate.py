"""Build immutable local candidate images and their manifest; no push/release/tag in Git."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export", action="store_true", help="Export both images for docker load")
    args = parser.parse_args()
    if command("git", "status", "--porcelain", capture=True):
        parser.exit(2, "Commit or isolate source changes before building a candidate\n")
    revision = command("git", "rev-parse", "HEAD", capture=True)
    if args.output.exists():
        parser.exit(2, "Candidate output already exists; do not overwrite a previous candidate\n")
    args.output.mkdir(parents=True)
    images = {}
    for kind, dockerfile in (("api", "infra/Dockerfile.api"), ("web", "infra/Dockerfile.frontend")):
        tag = f"sinoniks-{kind}:{revision}"
        build = ["docker", "build", "-f", dockerfile, "-t", tag,
                 "--label", "org.opencontainers.image.revision=" + revision]
        if kind == "api":
            build.extend(["--target", "runtime"])
        command(*build, ".")
        row = json.loads(command("docker", "image", "inspect", tag, capture=True))[0]
        images[kind] = {"tag": tag, "id": row["Id"], "architecture": row["Architecture"],
                        "os": row["Os"]}
    manifest = {"schema_version": 1, "git_revision": revision, "images": images,
                "verification": "Record test results for this exact revision in docs/PILOT.md"}
    if args.export:
        archive = args.output / "images.tar"
        command("docker", "save", "--output", str(archive.resolve()),
                *(image["tag"] for image in images.values()))
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest["archive_sha256"] = digest
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Candidate built locally; no registry push, deployment or release was performed")


if __name__ == "__main__":
    main()
