#!/usr/bin/env python3
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'python3'))

from smb.smb2_credit import main

if __name__ == '__main__':
    sys.exit(main())
