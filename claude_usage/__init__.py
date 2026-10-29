"""Parsing and aggregation library for Usage Lens, a dashboard over Claude
Code's local logs.

See serve.py / collect.py at the repo root for the two entry points.
"""

# The release, bumped with each entry in CHANGELOG.md and with the version in
# .claude-plugin/plugin.json; tests/test_version.py fails if the three disagree.
__version__ = "1.0.1"
