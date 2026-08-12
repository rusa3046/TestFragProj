"""Where a bottle can be bought.

Strictly downstream of everything else: commerce reads `fragrances` and
writes `products`, and nothing in the ingest, extraction, resolution or
ranking path imports this package. That is the trust rule expressed as a
dependency direction rather than as a promise.
"""
