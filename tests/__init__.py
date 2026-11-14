import os
import sys

# Make the workspace root importable regardless of how unittest discovers us.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
