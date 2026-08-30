"""Upload Datasheet PDFs to Cloudflare R2 (object storage) for SZ Procure.

Shadow-only tool. PDFs NEVER enter GitHub/Vercel — they live in R2 and are
referenced from product pages by HTTPS URL (datasheet_url -> gen_parts render).

Design:
  * One R2 object per SKU: key = `datasheets/<r2_key>.pdf` where r2_key is the
    mpn-based key from datasheet_map.csv (deterministic, one URL per part).
  * Idempotent: if the object already exists with the same byte size, SKIP.
  * Integrity: after upload, assert remote size == local size; with --verify,
    re-download and compare SHA256 (off by default; 767 MB re-fetch is heavy).
  * Dry-run (DEFAULT, and forced when R2 creds are absent): validates the whole
    plan, asserts every mapped local file exists + keys are unique + missing
    rows carry no URL, and reports totals. Makes NO network calls.

Credentials (set in the REAL upload environment, never committed):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
  R2_PUBLIC_BASE  (public URL base; must match the mapping's base)

Run:
  python tools/upload_datasheets.py                 # dry-run (safe, no creds needed)
  python tools/upload_datasheets.py --apply         # real upload (requires creds)
  python tools/upload_datasheets.py --apply --verify# real upload + SHA256 round-trip
"""
import csv, os, sys, hashlib, argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_CSV = "D:/SZ Procure/02_CLEAN/datasheet_map.csv"
PDF_DIR = "D:/SZ Procure/01_RAW/ASSET/datasheets"
REPORT = "D:/SZ Procure/04_Audit_Report/r2_upload_report.md"
PUBLIC_BASE = os.environ.get("SZ_R2_PUBLIC_BASE", "https://static.szprocure.com/datasheets").rstrip("/")


def load_map():
    rows = list(csv.DictReader(open(MAP_CSV, encoding="utf-8")))
    return rows


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dry_run(rows):
    print("=== DRY-RUN (no network, no upload) ===")
    mapped = [r for r in rows if r["status"] == "mapped"]
    missing = [r for r in rows if r["status"] == "missing"]
    # validation
    problems = []
    seen_keys = {}
    total_bytes = 0
    for r in mapped:
        lf = os.path.join(PDF_DIR, r["local_file"])
        if not os.path.exists(lf):
            problems.append(f"MISSING LOCAL FILE: {r['mpn']} -> {r['local_file']}")
            continue
        total_bytes += os.path.getsize(lf)
        k = r["r2_key"]
        if k in seen_keys:
            problems.append(f"DUPLICATE R2 KEY: {k} used by {seen_keys[k]} and {r['mpn']}")
        else:
            seen_keys[k] = r["mpn"]
        # url well-formed
        if not r["r2_url"].startswith("http"):
            problems.append(f"BAD URL: {r['mpn']} -> {r['r2_url']}")
    for r in missing:
        if r["r2_url"]:
            problems.append(f"MISSING SKU HAS URL (must be empty): {r['mpn']} -> {r['r2_url']}")
    print(f"- mapped SKUs to upload : {len(mapped)}")
    print(f"- missing (skipped)     : {len(missing)}")
    print(f"- unique R2 keys        : {len(seen_keys)}")
    print(f"- total bytes to upload : {total_bytes/1048576:.1f} MB")
    print(f"- validation problems   : {len(problems)}")
    for p in problems[:20]:
        print("    !", p)
    print("- Plan: PUT datasheets/<r2_key>.pdf for each mapped SKU (idempotent on size).")
    print("- With --apply: real upload to R2 bucket from env creds.")
    return problems


def real_upload(rows, verify):
    import boto3
    from botocore.client import Config
    acct = os.environ.get("R2_ACCOUNT_ID")
    ak = os.environ.get("R2_ACCESS_KEY_ID")
    sk = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET")
    if not (acct and ak and sk and bucket):
        print("ERROR: R2 creds missing (R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET). Aborting.")
        sys.exit(2)
    endpoint = f"https://{acct}.r2.cloudflarestorage.com"
    s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ak,
                     aws_secret_access_key=sk, config=Config(signature_version="s3v4"))
    results = []
    for r in rows:
        mpn = r["mpn"]
        if r["status"] != "mapped":
            results.append((mpn, r["r2_key"], "skipped_missing", "", ""))
            continue
        lf = os.path.join(PDF_DIR, r["local_file"])
        key = f"datasheets/{r['r2_key']}.pdf"
        local_sha = sha256_of(lf) if verify else ""
        # idempotent: skip if exists & same size
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            if head["ContentLength"] == os.path.getsize(lf):
                results.append((mpn, key, "skipped_exists", str(head["ContentLength"]), local_sha))
                continue
        except Exception:
            pass
        s3.upload_file(lf, bucket, key)
        # verify
        head = s3.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] != os.path.getsize(lf):
            results.append((mpn, key, "ERROR_size", str(head["ContentLength"]), local_sha))
            continue
        if verify:
            obj = s3.get_object(Bucket=bucket, Key=key)
            remote_sha = hashlib.sha256(obj["Body"].read()).hexdigest()
            if remote_sha != local_sha:
                results.append((mpn, key, "ERROR_sha256", str(head["ContentLength"]), remote_sha))
                continue
        results.append((mpn, key, "uploaded", str(head["ContentLength"]), local_sha))
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("# R2 Upload Report\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n\n")
        for mpn, key, st, size, sha in results:
            f.write(f"- {st}: {mpn} -> {key} ({size}B) sha={sha[:12]}\n")
    up = sum(1 for *_, st, _, _ in [(x[0], x[1], x[2], x[3], x[4]) for x in results] if st == "uploaded")
    print(f"Upload complete: {up} uploaded, {len(results)-up} skipped/other. Report: {REPORT}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually upload (requires R2 creds)")
    ap.add_argument("--verify", action="store_true", help="Re-download + SHA256 round-trip after upload")
    args = ap.parse_args()
    rows = load_map()
    if not args.apply:
        dry_run(rows)
        return 0
    real_upload(rows, args.verify)
    return 0


if __name__ == "__main__":
    sys.exit(main())
