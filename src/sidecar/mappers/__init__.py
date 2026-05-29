"""Pure-function mappers from Stripe payloads to Metronome request bodies.

Everything in this package is a pure function — no I/O, no globals, no time
dependence. Two reasons:

1. Pure functions are trivially testable (unit tests with dicts in, dicts out).
2. Customers forking this repo customize their integration by editing the
   mapping. Pure functions make it obvious what they're changing and what
   the downstream effect will be.
"""
