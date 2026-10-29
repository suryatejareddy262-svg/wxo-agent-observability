#!/usr/bin/env python3
"""
forwarder/manage_db.py
======================
Database Management CLI for WXO Agent Mapping Table.

Supports both AWS DynamoDB and PostgreSQL.

Commands:
  create-table     Create the wxo_agent_mappings table
  put              Insert or update an agent mapping
  get              Retrieve an agent mapping
  list             List all agent mappings
  delete           Delete an agent mapping
  seed             Seed database from config/agent_map.json

Usage:
  # DynamoDB:
  python3 src/forwarder/manage_db.py --dynamodb create-table
  python3 src/forwarder/manage_db.py --dynamodb seed
  python3 src/forwarder/manage_db.py --dynamodb put --agent customer-bot --wxo-agent-id <uuid> --tenant-id <tenant> --export-url <url>
  python3 src/forwarder/manage_db.py --dynamodb list

  # PostgreSQL:
  python3 src/forwarder/manage_db.py --postgres --db-url "postgresql://user:pass@localhost:5432/wxo" create-table
  python3 src/forwarder/manage_db.py --postgres --db-url "postgresql://user:pass@localhost:5432/wxo" seed
  python3 src/forwarder/manage_db.py --postgres list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "wxo_agent_mappings")


# =============================================================================
# DynamoDB Backend
# =============================================================================

def get_dynamo_resource(region: str = "us-east-1"):
    try:
        import boto3
        return boto3.resource("dynamodb", region_name=region)
    except ImportError:
        print("✗ boto3 is required for DynamoDB operations. Install with: pip install boto3")
        sys.exit(1)


def dynamo_create_table(region: str = "us-east-1", table_name: str = TABLE_NAME):
    dynamo = get_dynamo_resource(region)
    print(f"Creating DynamoDB table '{table_name}' in region {region}...")
    try:
        table = dynamo.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "agent_name", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "agent_name", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        print(f"✓ Table '{table_name}' created successfully.")
    except Exception as exc:
        if "ResourceInUseException" in str(exc) or "already exists" in str(exc):
            print(f"✓ Table '{table_name}' already exists.")
        else:
            print(f"✗ Failed to create table: {exc}")
            sys.exit(1)


def dynamo_put_item(
    agent_name: str,
    wxo_agent_id: str,
    wxo_tenant_id: str = "",
    wxo_otel_export_url: str = "",
    wxo_display_name: str = "",
    environment: str = "draft",
    region: str = "us-east-1",
    table_name: str = TABLE_NAME,
):
    dynamo = get_dynamo_resource(region)
    table = dynamo.Table(table_name)
    item = {
        "agent_name": agent_name,
        "wxo_agent_id": wxo_agent_id,
        "wxo_tenant_id": wxo_tenant_id,
        "wxo_otel_export_url": wxo_otel_export_url,
        "wxo_display_name": wxo_display_name or agent_name,
        "environment": environment,
    }
    table.put_item(Item=item)
    print(f"✓ Saved agent mapping '{agent_name}' → WXO ID '{wxo_agent_id}'")


def dynamo_list_items(region: str = "us-east-1", table_name: str = TABLE_NAME):
    dynamo = get_dynamo_resource(region)
    table = dynamo.Table(table_name)
    try:
        items = table.scan().get("Items", [])
        print(f"\nDynamoDB Table '{table_name}' ({len(items)} mappings):")
        print("─" * 80)
        for it in items:
            print(f"• Agent Name  : {it.get('agent_name')}")
            print(f"  WXO Agent ID: {it.get('wxo_agent_id')}")
            print(f"  Tenant ID   : {it.get('wxo_tenant_id')}")
            print(f"  Export URL  : {it.get('wxo_otel_export_url')}")
            print(f"  Environment : {it.get('environment', 'draft')}")
            print("─" * 80)
    except Exception as exc:
        print(f"✗ Failed to list items from {table_name}: {exc}")


def dynamo_delete_item(agent_name: str, region: str = "us-east-1", table_name: str = TABLE_NAME):
    dynamo = get_dynamo_resource(region)
    table = dynamo.Table(table_name)
    table.delete_item(Key={"agent_name": agent_name})
    print(f"✓ Deleted agent mapping for '{agent_name}'")


def dynamo_seed(region: str = "us-east-1", table_name: str = TABLE_NAME, seed_file: str = ""):
    file_path = seed_file
    if not file_path:
        for cand in (
            os.path.join(os.path.dirname(_repo_root), "agent_map.json"),
            os.path.join(os.path.dirname(_repo_root), "agent_map.json.example"),
            os.path.join(os.path.dirname(_repo_root), "config", "agent_map.json"),
            os.path.join(os.path.dirname(_repo_root), "config", "agent_map.json.example"),
        ):
            if os.path.exists(cand):
                file_path = cand
                break
    if not file_path or not os.path.exists(file_path):
        print(f"✗ Seed file not found: {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Seeding DynamoDB table '{table_name}' from {file_path}...")
    for k, v in data.items():
        if isinstance(v, str):
            v = {"wxo_agent_id": v}
        dynamo_put_item(
            agent_name=k,
            wxo_agent_id=v.get("wxo_agent_id", ""),
            wxo_tenant_id=v.get("wxo_tenant_id", ""),
            wxo_otel_export_url=v.get("wxo_otel_export_url", ""),
            wxo_display_name=v.get("wxo_display_name", k),
            environment=v.get("environment", "draft"),
            region=region,
            table_name=table_name,
        )
    print(f"✓ Successfully seeded {len(data)} agent mappings into DynamoDB.")


# =============================================================================
# PostgreSQL Backend
# =============================================================================

def get_postgres_connection(db_url: str):
    if not db_url:
        db_url = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("✗ PostgreSQL connection URL not specified. Provide --db-url or set POSTGRES_URL.")
        sys.exit(1)

    try:
        import psycopg2
        return psycopg2.connect(db_url)
    except ImportError:
        pass

    try:
        import psycopg
        return psycopg.connect(db_url)
    except ImportError:
        pass

    try:
        import pg8000.dbapi
        p = urllib.parse.urlparse(db_url)
        return pg8000.dbapi.connect(
            user=p.username,
            password=p.password,
            host=p.hostname,
            port=p.port or 5432,
            database=p.path.lstrip("/"),
        )
    except ImportError:
        print("✗ No PostgreSQL driver found. Please install psycopg2, psycopg, or pg8000:")
        print("    pip install psycopg2-binary  # or pip install psycopg")
        sys.exit(1)


def postgres_create_table(db_url: str):
    conn = get_postgres_connection(db_url)
    cur = conn.cursor()
    sql = """
    CREATE TABLE IF NOT EXISTS wxo_agent_mappings (
        agent_name VARCHAR(255) PRIMARY KEY,
        wxo_agent_id VARCHAR(255) NOT NULL,
        wxo_tenant_id VARCHAR(255),
        wxo_otel_export_url TEXT,
        wxo_display_name VARCHAR(255),
        environment VARCHAR(50) DEFAULT 'draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    cur.execute(sql)
    conn.commit()
    conn.close()
    print("✓ PostgreSQL table 'wxo_agent_mappings' created successfully.")


def postgres_put_item(
    agent_name: str,
    wxo_agent_id: str,
    wxo_tenant_id: str = "",
    wxo_otel_export_url: str = "",
    wxo_display_name: str = "",
    environment: str = "draft",
    db_url: str = "",
):
    conn = get_postgres_connection(db_url)
    cur = conn.cursor()
    sql = """
    INSERT INTO wxo_agent_mappings (agent_name, wxo_agent_id, wxo_tenant_id, wxo_otel_export_url, wxo_display_name, environment, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
    ON CONFLICT (agent_name) DO UPDATE SET
        wxo_agent_id = EXCLUDED.wxo_agent_id,
        wxo_tenant_id = EXCLUDED.wxo_tenant_id,
        wxo_otel_export_url = EXCLUDED.wxo_otel_export_url,
        wxo_display_name = EXCLUDED.wxo_display_name,
        environment = EXCLUDED.environment,
        updated_at = CURRENT_TIMESTAMP;
    """
    cur.execute(sql, (
        agent_name,
        wxo_agent_id,
        wxo_tenant_id,
        wxo_otel_export_url,
        wxo_display_name or agent_name,
        environment,
    ))
    conn.commit()
    conn.close()
    print(f"✓ Saved agent mapping '{agent_name}' → WXO ID '{wxo_agent_id}'")


def postgres_list_items(db_url: str = ""):
    conn = get_postgres_connection(db_url)
    cur = conn.cursor()
    cur.execute("SELECT agent_name, wxo_agent_id, wxo_tenant_id, wxo_otel_export_url, wxo_display_name, environment FROM wxo_agent_mappings ORDER BY agent_name;")
    rows = cur.fetchall()
    print(f"\nPostgreSQL 'wxo_agent_mappings' ({len(rows)} mappings):")
    print("─" * 80)
    for row in rows:
        print(f"• Agent Name  : {row[0]}")
        print(f"  WXO Agent ID: {row[1]}")
        print(f"  Tenant ID   : {row[2]}")
        print(f"  Export URL  : {row[3]}")
        print(f"  Display Name: {row[4]}")
        print(f"  Environment : {row[5]}")
        print("─" * 80)
    conn.close()


def postgres_delete_item(agent_name: str, db_url: str = ""):
    conn = get_postgres_connection(db_url)
    cur = conn.cursor()
    cur.execute("DELETE FROM wxo_agent_mappings WHERE agent_name = %s;", (agent_name,))
    conn.commit()
    conn.close()
    print(f"✓ Deleted agent mapping for '{agent_name}' from PostgreSQL.")


def postgres_seed(db_url: str = "", seed_file: str = ""):
    file_path = seed_file
    if not file_path:
        for cand in (
            os.path.join(os.path.dirname(_repo_root), "agent_map.json"),
            os.path.join(os.path.dirname(_repo_root), "agent_map.json.example"),
            os.path.join(os.path.dirname(_repo_root), "config", "agent_map.json"),
            os.path.join(os.path.dirname(_repo_root), "config", "agent_map.json.example"),
        ):
            if os.path.exists(cand):
                file_path = cand
                break
    if not file_path or not os.path.exists(file_path):
        print(f"✗ Seed file not found: {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Seeding PostgreSQL from {file_path}...")
    for k, v in data.items():
        if isinstance(v, str):
            v = {"wxo_agent_id": v}
        postgres_put_item(
            agent_name=k,
            wxo_agent_id=v.get("wxo_agent_id", ""),
            wxo_tenant_id=v.get("wxo_tenant_id", ""),
            wxo_otel_export_url=v.get("wxo_otel_export_url", ""),
            wxo_display_name=v.get("wxo_display_name", k),
            environment=v.get("environment", "draft"),
            db_url=db_url,
        )
    print(f"✓ Successfully seeded {len(data)} agent mappings into PostgreSQL.")


# =============================================================================
# Main CLI Entrypoint
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="WXO Agent Mapping Database Manager")
    parser.add_argument("--dynamodb", action="store_true", help="Use DynamoDB backend")
    parser.add_argument("--postgres", action="store_true", help="Use PostgreSQL backend")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"), help="AWS Region (default: us-east-1)")
    parser.add_argument("--table-name", default=TABLE_NAME, help=f"DynamoDB table name (default: {TABLE_NAME})")
    parser.add_argument("--db-url", default=os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL", ""), help="PostgreSQL connection string")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # create-table
    subparsers.add_parser("create-table", help="Create the mappings table")

    # put
    put_parser = subparsers.add_parser("put", help="Add or update an agent mapping")
    put_parser.add_argument("--agent", required=True, help="Platform agent identifier (e.g. customer-bot, service.name)")
    put_parser.add_argument("--wxo-agent-id", required=True, help="WXO Agent UUID")
    put_parser.add_argument("--tenant-id", default=os.environ.get("WXO_TENANT_ID", ""), help="WXO Tenant ID")
    put_parser.add_argument("--export-url", default=os.environ.get("WXO_OTEL_EXPORT_URL", ""), help="WXO OTel Export URL")
    put_parser.add_argument("--display-name", default="", help="Display label in WXO Dashboard")
    put_parser.add_argument("--environment", default="draft", choices=["draft", "live"], help="Environment name")

    # list
    subparsers.add_parser("list", help="List all agent mappings")

    # delete
    del_parser = subparsers.add_parser("delete", help="Delete an agent mapping")
    del_parser.add_argument("--agent", required=True, help="Agent name to delete")

    # seed
    seed_parser = subparsers.add_parser("seed", help="Seed mappings from config file")
    seed_parser.add_argument("--file", default="", help="Path to JSON file (default: config/agent_map.json)")

    args = parser.parse_args()

    # Default to DynamoDB if neither explicitly selected and in AWS environment, else error
    is_postgres = args.postgres or bool(args.db_url)
    is_dynamo = args.dynamodb or not is_postgres

    if is_postgres:
        if args.command == "create-table":
            postgres_create_table(args.db_url)
        elif args.command == "put":
            postgres_put_item(
                agent_name=args.agent,
                wxo_agent_id=args.wxo_agent_id,
                wxo_tenant_id=args.tenant_id,
                wxo_otel_export_url=args.export_url,
                wxo_display_name=args.display_name,
                environment=args.environment,
                db_url=args.db_url,
            )
        elif args.command == "list":
            postgres_list_items(args.db_url)
        elif args.command == "delete":
            postgres_delete_item(args.agent, args.db_url)
        elif args.command == "seed":
            postgres_seed(args.db_url, args.file)
    else:
        if args.command == "create-table":
            dynamo_create_table(args.region, args.table_name)
        elif args.command == "put":
            dynamo_put_item(
                agent_name=args.agent,
                wxo_agent_id=args.wxo_agent_id,
                wxo_tenant_id=args.tenant_id,
                wxo_otel_export_url=args.export_url,
                wxo_display_name=args.display_name,
                environment=args.environment,
                region=args.region,
                table_name=args.table_name,
            )
        elif args.command == "list":
            dynamo_list_items(args.region, args.table_name)
        elif args.command == "delete":
            dynamo_delete_item(args.agent, args.region, args.table_name)
        elif args.command == "seed":
            dynamo_seed(args.region, args.table_name, args.file)


if __name__ == "__main__":
    main()
