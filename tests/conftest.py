import sys
from pathlib import Path

# make the project importable when running `pytest` from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
