from __future__ import annotations

import argparse
import sys

from botocore.exceptions import BotoCoreError, ClientError

from oxypuro.aws_read import error_code, is_access_denied
from oxypuro.config import DATA_DIR, ConfigError, boto3_session, load_settings


def sync_prefix(s3, bucket: str, prefix: str) -> dict[str, int]:
    counts = {"listed": 0, "downloaded": 0, "skipped": 0, "bytes": 0}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key, size = obj["Key"], obj["Size"]
            counts["listed"] += 1
            local = DATA_DIR / key
            if local.exists() and local.stat().st_size == size:
                counts["skipped"] += 1
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(local.name + ".part")
            s3.download_file(bucket, key, str(tmp))
            tmp.replace(local)
            counts["downloaded"] += 1
            counts["bytes"] += size
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download new S3 raw objects to data/raw/.")
    parser.add_argument("--include-rejected", action="store_true", help="also sync rejected/")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        s3 = boto3_session(settings).client("s3")
        prefixes = ["raw/"] + (["rejected/"] if args.include_rejected else [])
        for prefix in prefixes:
            c = sync_prefix(s3, settings.bucket_name, prefix)
            print(
                f"{prefix:<10} listed {c['listed']}, downloaded {c['downloaded']}, "
                f"skipped {c['skipped']}, downloaded bytes {c['bytes']:,}"
            )
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    except ClientError as e:
        if is_access_denied(e):
            print(
                f"AccessDenied: s3:ListBucket/GetObject on {settings.bucket_name}", file=sys.stderr
            )
        else:
            print(f"AWS error: {error_code(e)}", file=sys.stderr)
        return 1
    except BotoCoreError as e:
        print(f"AWS client error (credentials or network): {e}", file=sys.stderr)
        return 1
    print(f"Local copy: {DATA_DIR.relative_to(DATA_DIR.parent)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
