import os 
import sys
import json
import math
import time
import queue
import random
import threading
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fbdaily as fb
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel
)