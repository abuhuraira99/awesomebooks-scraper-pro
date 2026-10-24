"""Build the client ZIP: ``python packaging/build_zip.py`` -> ``dist/AwesomeBooks-Scraper-Pro-v<version>.zip``.

The ZIP holds only what a client PC needs to run the app: the package, the two
launchers, the requirements, the upload templates and a plain-language guide.
Tests, docs, caches, databases and virtual environments are never included.

``python packaging/build_zip.py --update`` -> ``dist/AwesomeBooks-Scraper-Pro-v<version>-update.zip``
holds only the ``awesomebooks_pro`` folder, for a PC that already runs the app:
replace that folder and the data, settings and installed libraries stay as they are.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from awesomebooks_pro import __version__  # noqa: E402

TOP = f"AwesomeBooks Scraper Pro v{__version__}"
FILES = {
    "setup.bat": "setup.bat",
    "run.bat": "run.bat",
    "requirements.txt": "requirements.txt",
    "LICENSE": "LICENSE",
    "README.md": "README.md",
    "packaging/HOW TO USE.txt": "HOW TO USE.txt",
    "awesomebooks_pro/static/ISBN_Upload_Template.xlsx": "ISBN_Upload_Template.xlsx",
    "awesomebooks_pro/static/ISBN_Upload_Template.csv": "ISBN_Upload_Template.csv",
}
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def package_files(zf: zipfile.ZipFile, prefix: str) -> None:
    for path in sorted((ROOT / "awesomebooks_pro").rglob("*")):
        if path.is_file() and not SKIP_PARTS & set(path.parts) and path.suffix != ".pyc":
            zf.write(path, f"{prefix}{path.relative_to(ROOT).as_posix()}")


def build_update() -> Path:
    out = ROOT / "dist" / f"AwesomeBooks-Scraper-Pro-v{__version__}-update.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        package_files(zf, "")
    return out


def build() -> Path:
    out = ROOT / "dist" / f"AwesomeBooks-Scraper-Pro-v{__version__}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, dest in FILES.items():
            if dest.endswith((".bat", ".txt")):
                # cmd.exe needs CRLF line endings for goto labels; Notepad prefers them too.
                text = (ROOT / src).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                zf.writestr(f"{TOP}/{dest}", text)
            else:
                zf.write(ROOT / src, f"{TOP}/{dest}")
        package_files(zf, f"{TOP}/")
    return out


if __name__ == "__main__":
    path = build_update() if "--update" in sys.argv[1:] else build()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    print(f"{path}  ({path.stat().st_size / 1024:.0f} KB, {len(names)} files)")
