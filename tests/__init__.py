"""Test package.

Making tests/ a package puts the repo root on sys.path (so `gitsync` imports
without installing anything) and lets the test modules share conftest helpers.
"""
