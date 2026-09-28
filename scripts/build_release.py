"""Create a manual-install archive without development state or credentials."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
version = json.loads(
    (root / "custom_components/better_proxy/manifest.json").read_text()
)["version"]
output = root / "dist" / f"better-proxy-{version}.zip"
output.parent.mkdir(exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for path in sorted((root / "custom_components" / "better_proxy").rglob("*")):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix in {".py", ".js", ".json", ".png"}
        ):
            archive.write(path, path.relative_to(root))
    archive.write(root / "README.md", "README.md")
    archive.write(root / "LICENSE", "LICENSE")
    for path in sorted((root / "docs" / "screenshots").glob("*.jpg")):
        archive.write(path, path.relative_to(root))
print(output)
