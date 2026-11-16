"""Run pyx12 validation on an X12 file and print the 999 ack and JSON errors."""

from __future__ import annotations

import io
import sys

import pyx12.params
import pyx12.x12n_document


def main(path: str) -> None:
    param = pyx12.params.params()
    fd_999 = io.StringIO()
    fd_json = io.StringIO()
    with open(path, encoding="ascii") as src:
        ok = pyx12.x12n_document.x12n_document(
            param=param,
            src_file=src,
            fd_997=fd_999,
            fd_html=None,
            fd_xmldoc=None,
            fd_json=fd_json,
        )
    print("VALID:", ok)
    print("===== 999 =====")
    fd_999.seek(0)
    print(fd_999.read())
    print("===== JSON =====")
    fd_json.seek(0)
    print(fd_json.read())


if __name__ == "__main__":
    main(sys.argv[1])
