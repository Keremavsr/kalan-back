"""Check how kalan handles a folder of your own statement PDFs, without showing their contents.

Usage: ``python tools/check_real.py <folder> [--names]``

For every PDF (sorted by file name) it prints only: a running number, the parser id, the number of
transactions, the number of installment plans, the reconciliation status and the difference in
minor units. No description, name, card number or amount is printed. File names are hidden unless
``--names`` is given, because they can contain personal data. Errors are reported by type only.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalan.pipeline import parse_pdf  # noqa: E402


def check_folder(folder: Path, show_names: bool = False) -> list[str]:
    lines = []
    pdfs = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")
    for n, pdf in enumerate(pdfs, start=1):
        label = f"{n:03d}" + (f" {pdf.name}" if show_names else "")
        try:
            st = parse_pdf(pdf)
        except Exception as exc:  # report the type only: messages may quote statement text
            lines.append(f"{label}  error={type(exc).__name__}")
            continue
        plans = {t.installment.plan_id for t in st.transactions if t.installment is not None}
        r = st.reconciliation
        status = r.status if r else "none"
        diff = r.difference if r else 0
        failed = ",".join(c.name for c in r.checks if c.status != "ok") if r else ""
        lines.append(
            f"{label}  parser={st.parser_id}  transactions={len(st.transactions)}  plans={len(plans)}  "
            f"reconciliation={status}  difference={diff}" + (f"  failed={failed}" if failed else "")
        )
    return lines


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2])
        return 2
    folder = Path(args[0])
    if not folder.is_dir():
        print("not a folder")
        return 2
    for line in check_folder(folder, show_names="--names" in argv):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
