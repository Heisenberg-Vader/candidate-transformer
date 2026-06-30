"""Root conftest: ensures the repo root is importable as the test rootdir.

Its mere presence at the repo root puts that directory on ``sys.path`` (via
pytest's prepend import mode), so ``import transformer`` works without an
editable install.
"""
