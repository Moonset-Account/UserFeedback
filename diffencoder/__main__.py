import os
import sys

try:  # package mode: python -m diffencoder
    from .cli import main
except ImportError:  # direct script mode: python diffencoder/__main__.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from diffencoder.cli import main

raise SystemExit(main())
