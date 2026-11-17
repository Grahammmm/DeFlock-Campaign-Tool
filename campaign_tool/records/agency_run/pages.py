"""One page-numbered text set per agency (REC-PAGES-1). Never runs OCR; read-only on the root."""
import argparse
import csv
import hashlib
import json
import os
import re
import sys

from .evidence_index import HEADER, MAX_PDF, clean, inside, write_private

INVENTORY = "doc_id title received_date source pages sha256 text_layer tier request_ref notes".split()
MAX_ROWS = 5000
MAX_OCR = 20 * 1024 * 1024
MIN_NATIVE = 20
# Accepted marker grammar of the campaign OCR text; a later format change edits only this constant.
OCR_MARKER = r"(?im)^[ \t=\-#\[]*page[ \t]+(\d+)(?:[ \t]+of[ \t]+\d+)?[ \t=\-#\]]*$"


class Refuse(Exception):
    pass


def read_index(path):
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle, delimiter="\t"))
    except (OSError, UnicodeError, csv.Error):
        raise Refuse("index_invalid")
    if not rows or rows[0] != HEADER or len(rows) > MAX_ROWS + 1 or any(len(r) != len(HEADER) for r in rows):
        raise Refuse("index_invalid")
    return [dict(zip(HEADER, r)) for r in rows[1:]]


def ocr_pages(path, doc, pages, failures):
    """Return {page: text} from a campaign OCR text file, or {} when none exists."""
    if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) > MAX_OCR:
        return {}
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read().replace("\r\n", "\n")
    except (OSError, UnicodeError):
        failures.append("ocr_unreadable:" + doc)
        return {}
    marks = list(re.finditer(OCR_MARKER, text))
    if not marks:
        failures.append("ocr_markers_unrecognised:" + doc)
        return {}
    found = {}
    for i, m in enumerate(marks):
        n = int(m.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        if not 1 <= n <= pages:
            failures.append("ocr_page_out_of_range:" + doc)
        elif n in found:
            failures.append("ocr_page_duplicate:" + doc)
        else:
            found[n] = text[m.end():end].strip("\n")
    return found


def process(row, doc, root, ocr_dir, failures):
    """Validate one original PDF and return its page texts, or None on failure."""
    from pypdf import PdfReader
    real = inside(root, row["path"])
    if real is None or not os.path.isfile(real) or os.path.getsize(real) > MAX_PDF:
        failures.append("path_invalid:" + doc)
        return None
    with open(real, "rb") as handle:
        data = handle.read()
    digest = hashlib.sha256(data).hexdigest()
    before = len(failures)
    if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
        failures.append("hash_unknown:" + doc)
    elif row["sha256"] != digest:
        failures.append("hash_mismatch:" + doc)
    if not re.fullmatch(r"[0-9]+", row["pdf_pages"]):
        failures.append("page_count_unknown:" + doc)
    try:
        import io
        reader = PdfReader(io.BytesIO(data))
        count = len(reader.pages)
    except Exception:
        failures.append("pdf_unreadable:" + doc)
        return None
    if len(failures) > before:
        return None
    if int(row["pdf_pages"]) != count:
        failures.append("page_count_mismatch:" + doc)
        return None
    native = {}
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if len("".join(text.split())) >= MIN_NATIVE:
            native[i] = text
    ocr = {}
    if ocr_dir:
        ocr = ocr_pages(os.path.join(ocr_dir, digest + ".txt"), doc, count, failures)
    return {"digest": digest, "count": count, "native": native,
            "ocr": {n: t for n, t in ocr.items() if n not in native and t.strip()}}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="records agency-pages", description=__doc__)
    ap.add_argument("--index", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ocr-dir")
    args = ap.parse_args(argv)
    failures = []
    try:
        if not os.path.isdir(args.root):
            raise Refuse("root_missing")
        root, out = os.path.realpath(args.root), os.path.realpath(args.out)
        if out == root or os.path.commonpath([root, out]) == root:
            raise Refuse("out_inside_root")
        if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
            raise Refuse("out_not_empty")
        rows = read_index(args.index)
        docs, skipped = [], 0
        for row in rows:
            if row["role"] == "original" and row["path"].lower().endswith(".pdf"):
                doc = "D%d" % (len(docs) + 1)
                docs.append((doc, row, process(row, doc, root, args.ocr_dir, failures)))
            else:
                skipped += 1
        if failures:
            raise Refuse("\n".join("agency-pages: " + f for f in failures))
    except Refuse as exc:
        text = str(exc)
        print(text if text.startswith("agency-pages: ") else "agency-pages: " + text, file=sys.stderr)
        return 1
    os.makedirs(os.path.join(out, "pages"), mode=0o700, exist_ok=True)
    inventory, written, blank = ["\t".join(INVENTORY)], 0, 0
    for doc, row, info in docs:
        missing, layers = [], set()
        for n in range(1, info["count"] + 1):
            if n in info["native"]:
                text, layers = info["native"][n].rstrip("\n"), layers | {"native"}
            elif n in info["ocr"]:
                text, layers = info["ocr"][n].rstrip("\n"), layers | {"ocr"}
            else:
                missing.append("p.%d" % n)
                continue
            write_private(os.path.join(out, "pages", "%s_p%03d.txt" % (doc, n)), "=== %s p.%d ===\n%s\n" % (doc, n, text))
            written += 1
        blank += len(missing)
        notes = "no text layer: " + ", ".join(missing) if missing else ""
        layer = "+".join(x for x in ("native", "ocr") if x in layers) or "none"
        inventory.append("\t".join(clean(c) for c in (
            doc, os.path.basename(row["path"]), row["date_pacific"], row["path"], info["count"],
            info["digest"], layer, "unclassified", row["request_ref"], notes)))
    write_private(os.path.join(out, "inventory.tsv"), "\n".join(inventory) + "\n")
    print(json.dumps({"ok": True, "documents": len(docs), "pages_written": written,
                      "no_text_pages": blank, "skipped_not_pdf": skipped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
