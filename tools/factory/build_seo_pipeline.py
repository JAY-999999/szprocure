"""
SZ Procure — SEO post-build pipeline orchestrator (Phase 3, YELLOW-1/2/3).

Runs the two narrow post-processors AFTER gen_parts.py has produced the build:

  1. apply_index_policy.apply_to_site  (robots meta / Product JSON-LD / canonical)
  2. sitemap_prune.rebuild_sitemap      (homepage + 6 static, noindex exclusion)

Both consume index_policy.classify_parts() as the single source of truth, so
the robots <meta> and the sitemap are always policy-consistent. gen_parts.py
is NEVER invoked or modified by this module.

Usage:
  python build_seo_pipeline.py --site-root . --parts-json parts.json [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import apply_index_policy as ap
import index_policy as ip
import sitemap_prune as sp


def run(site_root: str, parts_json_path: str, dry_run: bool = False) -> dict:
    with open(parts_json_path, encoding="utf-8") as fh:
        records = json.load(fh)
    classified = ip.classify_parts(records)

    summary = {
        "classified_total": len(classified),
        "index": sum(1 for c in classified.values() if c.indexable),
        "noindex": sum(1 for c in classified.values() if not c.indexable),
        "noindex_reasons": sorted(
            {c.reason for c in classified.values() if not c.indexable}),
    }
    summary["html"] = ap.apply_to_site(site_root, parts_json_path, dry_run=dry_run)
    summary["sitemap"] = sp.rebuild_sitemap(site_root, parts_json_path, dry_run=dry_run)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="SZ Procure SEO post-build pipeline")
    p.add_argument("--site-root", default=".")
    p.add_argument("--parts-json", default="parts.json")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    report = run(args.site_root, args.parts_json, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
