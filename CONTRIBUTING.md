# Contributing

The fastest way to help is to add DAX coverage, one function at a time.

1. Add the function to `src/opentabular/compiler.py`.
2. Add a test with a hand-calculated expected value to `tests/test_compiler.py`, and,
   if it touches joins or time, a comparison against hand-written SQL in
   `tests/test_differential.py`.
3. Document it in `docs/dax-coverage.md`, including any difference from Power BI.

A function that cannot be translated faithfully must raise `UnsupportedError`. Returning
a number that Power BI would not return is a bug; refusing to compile is not.

Run `pytest` before opening a pull request.
