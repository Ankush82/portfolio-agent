# Marker file: makes tests/support a regular package so test imports
# of `fake_infrastructure` resolve via either `tests.support.fake_infrastructure`
# or `from fake_infrastructure import ...` depending on how pytest is
# configured. The fake itself (tests/support/fake_infrastructure.py)
# is the real artifact.
