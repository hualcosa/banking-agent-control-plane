#!/usr/bin/env python3
"""Idle monthly cost of the AWS deployment, priced from the live AWS Price List.

"Idle" = deployed, zero traffic. Only things billed by the hour or by the stored
GB count; per-request/per-GB-processed charges are zero at idle and left out.

Prices come from the public Price List offer files (no AWS credentials needed)
and are cached per publication day in ~/.cache/trail-idle-cost. The EC2 offer
(NAT Gateway) is a 200 MB CSV, so the first run takes ~1-2 min.

    python3 infra-cdk/cost/idle_cost.py                    # the full planned AWS stack
    python3 infra-cdk/cost/idle_cost.py --scenario network # what `cdk deploy` creates today
    python3 infra-cdk/cost/idle_cost.py --endpoint-azs 1 --multi-az --json
"""

import argparse
import csv
import datetime
import io
import json
import pathlib
import urllib.request

BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"
REGION, PREFIX = "sa-east-1", "SAE1-"
CACHE = pathlib.Path.home() / ".cache" / "trail-idle-cost"
HOURS_PER_MONTH = 730


def _get(url: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = (
        CACHE
        / f"{datetime.date.today()}-{url.replace(BASE, '').strip('/').replace('/', '_')}"
    )
    if not f.exists():
        f.write_bytes(urllib.request.urlopen(url, timeout=600).read())
    return f.read_bytes()


def _offer(service: str, region: str = REGION) -> dict:
    return json.loads(_get(f"{BASE}/{service}/current/{region}/index.json"))


def _price(offer: dict, usagetype: str, **attrs) -> float:
    """First-tier on-demand USD price for a usagetype (plus optional attribute match)."""
    for sku, p in offer["products"].items():
        a = p["attributes"]
        if a.get("usagetype") != usagetype or any(
            a.get(k) != v for k, v in attrs.items()
        ):
            continue
        for term in offer["terms"]["OnDemand"].get(sku, {}).values():
            for dim in term["priceDimensions"].values():
                if dim.get("beginRange", "0") == "0":
                    return float(dim["pricePerUnit"]["USD"])
    raise LookupError(f"no price for {usagetype} {attrs}")


def _nat_prices() -> dict:
    # ponytail: streams the whole EC2 CSV once a day; the Pricing API would be faster but needs credentials.
    url = f"{BASE}/AmazonEC2/current/{REGION}/index.csv"
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{datetime.date.today()}-nat.json"
    if f.exists():
        return json.loads(f.read_text())
    want, found = {f"{PREFIX}NatGateway-Hours"}, {}
    with urllib.request.urlopen(url, timeout=600) as r:
        lines = io.TextIOWrapper(r, encoding="utf-8")
        for _ in range(5):  # metadata preamble before the header row
            next(lines)
        rows = csv.reader(lines)
        head = next(rows)
        ut, price, term = (
            head.index("usageType"),
            head.index("PricePerUnit"),
            head.index("TermType"),
        )
        for row in rows:
            if row[ut] in want and row[term] == "OnDemand":
                found[row[ut]] = float(row[price])
                if len(found) == len(want):
                    break
    f.write_text(json.dumps(found))
    return found


def fetch_prices() -> dict:
    vpc, rds = _offer("AmazonVPC"), _offer("AmazonRDS")
    sm, kms, ecr, cw = (
        _offer("AWSSecretsManager"),
        _offer("awskms"),
        _offer("AmazonECR"),
        _offer("AmazonCloudWatch"),
    )
    agentcore_sa = _offer("AmazonBedrockAgentCore")
    runtime_region = REGION
    try:
        vcpu = _price(agentcore_sa, f"{PREFIX}Runtime:Consumption-based:vCPU")
        mem = _price(agentcore_sa, f"{PREFIX}Runtime:Consumption-based:Memory")
    except LookupError:  # sa-east-1 Runtime SKUs are not in the Price List yet
        use1, runtime_region = (
            _offer("AmazonBedrockAgentCore", "us-east-1"),
            "us-east-1 (fallback)",
        )
        vcpu = _price(use1, "USE1-Runtime:Consumption-based:vCPU")
        mem = _price(use1, "USE1-Runtime:Consumption-based:Memory")
    pg = {"databaseEngine": "PostgreSQL"}
    prices = {
        "published": vpc["publicationDate"],
        "nat_hour": _nat_prices()[f"{PREFIX}NatGateway-Hours"],
        "public_ipv4_hour": _price(vpc, f"{PREFIX}PublicIPv4:InUseAddress"),
        "interface_endpoint_az_hour": _price(vpc, f"{PREFIX}VpcEndpoint-Hours"),
        "rds_hour": {},
        "rds_gp3_gb_month": _price(rds, f"{PREFIX}RDS:GP3-Storage", **pg),
        "rds_gp3_gb_month_multi_az": _price(
            rds, f"{PREFIX}RDS:Multi-AZ-GP3-Storage", **pg
        ),
        "rds_backup_gb_month": _price(rds, f"{PREFIX}RDS:ChargedBackupUsage"),
        "secret_month": _price(sm, f"{PREFIX}AWSSecretsManager-Secrets"),
        "kms_key_month": _price(kms, "sa-east-1-KMS-Keys"),
        "ecr_gb_month": _price(ecr, f"{PREFIX}TimedStorage-ByteHrs"),
        "logs_gb_month": _price(cw, f"{PREFIX}TimedStorage-ByteHrs"),
        "alarm_month": _price(cw, f"{PREFIX}CW:AlarmMonitorUsage"),
        "runtime_vcpu_hour": vcpu,
        "runtime_mem_gb_hour": mem,
        "runtime_price_region": runtime_region,
    }
    for cls in ("db.t4g.micro", "db.t4g.small", "db.t4g.medium"):
        prices["rds_hour"][cls] = {
            "single": _price(rds, f"{PREFIX}InstanceUsage:{cls}", **pg),
            "multi": _price(rds, f"{PREFIX}Multi-AZUsage:{cls}", **pg),
        }
    return prices


def estimate(p: dict, a: argparse.Namespace) -> list[tuple[str, str, float]]:
    h = a.hours
    lines = [
        ("VPC", f"{a.nat} NAT Gateway × {h} h", a.nat * p["nat_hour"] * h),
        (
            "VPC",
            f"{a.nat} public IPv4 (NAT EIP) × {h} h",
            a.nat * p["public_ipv4_hour"] * h,
        ),
        (
            "VPC",
            f"{a.endpoints} interface endpoints × {a.endpoint_azs} AZ × {h} h",
            a.endpoints * a.endpoint_azs * p["interface_endpoint_az_hour"] * h,
        ),
        ("VPC", "S3 gateway endpoint, VPC, subnets, IGW", 0.0),
    ]
    if a.scenario == "planned":
        rds = p["rds_hour"][a.rds_class]["multi" if a.multi_az else "single"]
        gp3 = p["rds_gp3_gb_month_multi_az" if a.multi_az else "rds_gp3_gb_month"]
        idle_session_hours = a.idle_session_hours
        lines += [
            (
                "RDS",
                f"{a.rds_class} {'Multi-AZ' if a.multi_az else 'Single-AZ'} × {h} h",
                rds * h,
            ),
            ("RDS", f"{a.storage_gb} GB gp3", a.storage_gb * gp3),
            (
                "RDS",
                f"{a.backup_gb_over_free} GB backup beyond free allocation",
                a.backup_gb_over_free * p["rds_backup_gb_month"],
            ),
            (
                "Secrets Manager",
                f"{a.secrets} secret(s)",
                a.secrets * p["secret_month"],
            ),
            (
                "KMS",
                f"{a.kms_keys} customer-managed key(s)",
                a.kms_keys * p["kms_key_month"],
            ),
            ("ECR", f"{a.ecr_gb} GB image storage", a.ecr_gb * p["ecr_gb_month"]),
            (
                "CloudWatch",
                f"{a.log_gb} GB stored logs/traces",
                a.log_gb * p["logs_gb_month"],
            ),
            (
                "CloudWatch",
                f"{a.alarms} standard alarm(s)",
                a.alarms * p["alarm_month"],
            ),
            (
                "AgentCore Runtime",
                f"{idle_session_hours} idle session-h × {a.session_mem_gb} GB (no CPU when idle)",
                idle_session_hours * a.session_mem_gb * p["runtime_mem_gb_hour"],
            ),
            ("Cognito", "user pool, 0 MAU (Essentials/Lite free tier)", 0.0),
            ("Bedrock models", "no invocations", 0.0),
        ]
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--scenario", choices=["network", "planned"], default="planned")
    ap.add_argument("--hours", type=float, default=HOURS_PER_MONTH)
    ap.add_argument("--nat", type=int, default=1)
    ap.add_argument(
        "--endpoints",
        type=int,
        default=3,
        help="interface endpoints (ECR dkr, ECR api, Logs)",
    )
    ap.add_argument("--endpoint-azs", type=int, default=3)
    ap.add_argument(
        "--rds-class",
        default="db.t4g.micro",
        choices=["db.t4g.micro", "db.t4g.small", "db.t4g.medium"],
    )
    ap.add_argument("--multi-az", action="store_true")
    ap.add_argument("--storage-gb", type=float, default=20)
    ap.add_argument("--backup-gb-over-free", type=float, default=0)
    ap.add_argument("--secrets", type=int, default=1, help="RDS-managed master secret")
    ap.add_argument("--kms-keys", type=int, default=0)
    ap.add_argument("--ecr-gb", type=float, default=0.5)
    ap.add_argument("--log-gb", type=float, default=1)
    ap.add_argument("--alarms", type=int, default=0)
    ap.add_argument(
        "--idle-session-hours",
        type=float,
        default=0,
        help="Runtime sessions left open while idle",
    )
    ap.add_argument("--session-mem-gb", type=float, default=0.5)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    prices = fetch_prices()
    lines = estimate(prices, a)
    total = sum(c for *_, c in lines)
    if a.json:
        print(
            json.dumps(
                {"prices": prices, "lines": lines, "total_usd_month": round(total, 2)},
                indent=2,
            )
        )
        return
    print(
        f"Idle cost · {REGION} · scenario={a.scenario} · Price List published {prices['published']}"
    )
    for svc, what, cost in lines:
        print(f"  {svc:<18} {what:<58} ${cost:>8.2f}")
    print(f"  {'TOTAL / month':<77} ${total:>8.2f}")
    if a.scenario == "planned" and "fallback" in prices["runtime_price_region"]:
        print(
            "  note: AgentCore Runtime has no sa-east-1 SKU in the Price List; memory rate is us-east-1's."
        )


if __name__ == "__main__":
    main()
