#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""``python -m pysnmp.linkreport`` support."""

import sys

from pysnmp.linkreport.cli import main

if __name__ == "__main__":
    sys.exit(main())
