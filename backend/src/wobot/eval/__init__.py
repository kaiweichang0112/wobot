"""Evaluation harness: labelled datasets scored against an index version with RAGAS."""

import os

# RAGAS posts a usage event for every score to its maker's server unless this is set; it
# reads the variable when it first scores, so setting it before any metric runs is enough.
os.environ["RAGAS_DO_NOT_TRACK"] = "true"
