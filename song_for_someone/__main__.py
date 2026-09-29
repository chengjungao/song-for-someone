# -*- coding: utf-8 -*-
"""支持 python -m song_for_someone 直接调用。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
