import os
import sys

# Add root directory to sys.path for Vercel import resolution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app
