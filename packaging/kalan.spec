# PyInstaller spec: one executable `kalan` (CLI + `serve` + `mcp`) for Windows, macOS and Linux.
#
#   python -m pip install -e ".[build]"
#   pyinstaller packaging/kalan.spec --clean --noconfirm
#
# Output: dist/kalan.exe (Windows) or dist/kalan. PDF text comes from pypdfium2 (Apache-2.0 / BSD-3);
# PyMuPDF (AGPL) is never bundled. While src/kalan/pdf.py still imports PyMuPDF the build stops, unless
# KALAN_DEV_BUILD=1 is set for a local test build that must not be published.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is set by PyInstaller)
SRC = ROOT / "src"
PDF_SOURCE = (SRC / "kalan" / "pdf.py").read_text(encoding="utf-8")
USES_PYMUPDF = "import pymupdf" in PDF_SOURCE or "import fitz" in PDF_SOURCE
DEV_BUILD = os.environ.get("KALAN_DEV_BUILD") == "1"

if USES_PYMUPDF and not DEV_BUILD:
    raise SystemExit(
        "src/kalan/pdf.py still imports PyMuPDF (AGPL), which kalan does not ship. Switch it to pypdfium2, "
        "or set KALAN_DEV_BUILD=1 for a local test build that is never published."
    )

hiddenimports = [
    *collect_submodules("kalan"),  # parsers and CLI commands are discovered with pkgutil at run time
    *collect_submodules("uvicorn"),
    # mcp.cli needs typer and mcp.server.fastmcp only raises (removed in mcp 2): skip both
    *collect_submodules("mcp", filter=lambda n: not n.startswith(("mcp.cli", "mcp.server.fastmcp"))),
    *collect_submodules("mcp_types"),
    *collect_submodules("keyring.backends"),
]
datas = [
    *collect_data_files("kalan"),  # migrations/*.sql, templates/*.yaml, template.schema.json
    *copy_metadata("kalan"),
    *copy_metadata("keyring"),  # keyring finds its backends through entry points
    *copy_metadata("mcp"),
]
excludes = ["tkinter", "pytest", "ruff", "httpx2.tests", "IPython"]
if not USES_PYMUPDF:
    excludes += ["pymupdf", "fitz", "pymupdf_fonts"]
    hiddenimports += collect_submodules("pypdfium2")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="kalan",
    console=True,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
)
