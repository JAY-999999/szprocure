"""
Test-only build helper for tests/test_sitemap_sharding.py.

Loads the REAL gen_parts module and invokes its main() in-process, but bypasses
ONLY the production-source PATH guard (validate_production_source) so the sitemap
sharding logic can be exercised against a synthetic master placed outside
data/production/. The synthetic MPNs still pass detect_synthetic_mpn, so the rest
of the production safety net stays intact. gen_parts.py itself is NOT modified.
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(REPO, "gen_parts.py")


def main():
    spec = importlib.util.spec_from_file_location("gen_parts_under_test", GEN)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    # Test-only bypass: allow a synthetic master outside data/production/.
    g.validate_production_source = lambda csv_path: True
    print("WRAPPER ARGV:", sys.argv, file=sys.stderr)
    try:
        g.main()
    except SystemExit as e:
        sys.stderr.write(f"BUILD ABORTED (SystemExit code={e.code})\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
