#!/usr/bin/env python3
"""Ionic Data Platform Community Connector CLI

Provides a CLI for querying the Ionic Data Platform, performing indexed term searches using
REGEXP syntax across various token types, and inspecting available catalogs and tables.
Supports direct execution via the vendor DBAPI driver when installed, falling back to HTTP
statement endpoint or mock responses for offline testing.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). This connector predates the shared
# click-based _lib framework used elsewhere in this repo (plain argparse,
# direct DBAPI/HTTP execution, no MCP api_proxy) -- HARD_CAP is enforced via
# a custom argparse type (_bounded_limit) instead of the click library's
# IntRange type.
#
# DEFAULT_LIMIT == HARD_CAP (both 100) is a deliberate choice, not an
# oversight: --limit already defaulted to 100 before this fix, and lowering
# it now (e.g. to the 5-25 CONTRIBUTING.md suggests for a fresh default)
# would silently return fewer rows than before to any existing caller that
# omits --limit. Preserving the pre-existing default was judged less
# surprising than a smaller one, even though it means there's no headroom
# between "default" and "max" the way other connectors in this repo have.
DEFAULT_LIMIT = 100
HARD_CAP = 100


def _bounded_limit(value: str) -> int:
    """argparse type for --limit: an int in [1, HARD_CAP], rejected (not
    silently clamped) outside that range -- the argparse equivalent of the
    IntRange-based bound (via the click library) used by every other
    connector in this repo.
    """
    try:
        n = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from e
    if not (1 <= n <= HARD_CAP):
        raise argparse.ArgumentTypeError(
            f"limit must be between 1 and {HARD_CAP} (hard cap), got {n}"
        )
    return n


def load_config(config_arg: Optional[str] = None) -> Dict[str, Any]:
    """Load configuration from environment variables and optional --config input."""
    config: Dict[str, Any] = {
        "host": os.environ.get("IONIC_HOST"),
        "port": int(os.environ.get("IONIC_PORT", 443)),
        "auth_realm": os.environ.get("IONIC_AUTH_REALM", "ionic"),
        "catalog": os.environ.get("IONIC_CATALOG", "ionic"),
        "schema": os.environ.get("IONIC_SCHEMA", "default"),
        "auth_url": os.environ.get("IONIC_AUTH_URL"),
        "auth_flow": os.environ.get("IONIC_AUTH_FLOW", "SERVICE_ACCOUNT"),
        "client_id": os.environ.get("IONIC_CLIENT_ID"),
        "client_secret": os.environ.get("IONIC_CLIENT_SECRET"),
        "username": os.environ.get("IONIC_USERNAME"),
        "password": os.environ.get("IONIC_PASSWORD"),
        "session_username": os.environ.get("IONIC_SESSION_USERNAME"),
        "ssl_verification": os.environ.get("IONIC_SSL_VERIFICATION", "FULL"),
        "ca_bundle": os.environ.get("IONIC_CA_BUNDLE"),
        "certfile": os.environ.get("IONIC_CERTFILE") or os.environ.get("IONIC_CERT_PATH"),
        "keyfile": os.environ.get("IONIC_KEYFILE") or os.environ.get("IONIC_KEY_PATH"),
        "keyfile_pw": os.environ.get("IONIC_KEYFILE_PW"),
        "source": os.environ.get("IONIC_SOURCE", "crogl"),
        "client_tags": ["ad-hoc", "ad-hoc:crogl"],
        "session_properties": {},
    }

    raw_tags = os.environ.get("IONIC_CLIENT_TAGS")
    if raw_tags:
        try:
            config["client_tags"] = json.loads(raw_tags)
        except Exception:
            config["client_tags"] = [t.strip() for t in raw_tags.split(",")]

    raw_props = os.environ.get("IONIC_SESSION_PROPERTIES")
    if raw_props:
        try:
            config["session_properties"] = json.loads(raw_props)
        except Exception:
            pass

    if config_arg:
        cfg_obj = None
        if os.path.exists(config_arg):
            with open(config_arg, "r", encoding="utf-8") as f:
                cfg_obj = json.load(f)
        else:
            try:
                cfg_obj = json.loads(config_arg)
            except Exception:
                pass
        if isinstance(cfg_obj, dict):
            for k, v in cfg_obj.items():
                if v is not None:
                    config[k] = v

    return config


def create_ssl_context(config: Dict[str, Any]) -> ssl.SSLContext:
    """Create SSL Context based on configuration."""
    verification = str(config.get("ssl_verification") or "FULL").upper()
    if verification == "NONE":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ca_bundle = config.get("ca_bundle")
    if ca_bundle and os.path.exists(ca_bundle):
        ctx = ssl.create_default_context(cafile=ca_bundle)
    else:
        ctx = ssl.create_default_context()

    certfile = config.get("certfile")
    keyfile = config.get("keyfile")
    if certfile and os.path.exists(certfile):
        ctx.load_cert_chain(
            certfile=certfile,
            keyfile=keyfile if keyfile and os.path.exists(keyfile) else None,
            password=config.get("keyfile_pw"),
        )

    return ctx


def acquire_oauth_token(config: Dict[str, Any]) -> str:
    """Acquire OAuth2 Bearer token from auth_url endpoint."""
    auth_url = config.get("auth_url")
    if not auth_url:
        raise ValueError("Missing required configuration parameter: auth_url")

    auth_flow = str(config.get("auth_flow") or "SERVICE_ACCOUNT").upper()
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")

    payload: Dict[str, str] = {}
    if auth_flow in ("SERVICE_ACCOUNT", "EXCHANGE", "DEVICE"):
        payload["grant_type"] = "client_credentials"
        if client_id:
            payload["client_id"] = client_id
        if client_secret:
            payload["client_secret"] = client_secret
        if config.get("session_username"):
            payload["subject"] = config["session_username"]
    elif auth_flow in ("DIRECT", "IMPERSONATION"):
        payload["grant_type"] = "password"
        if client_id:
            payload["client_id"] = client_id
        if client_secret:
            payload["client_secret"] = client_secret
        if config.get("username"):
            payload["username"] = config["username"]
        if config.get("password"):
            payload["password"] = config["password"]
    else:
        payload["grant_type"] = "client_credentials"
        if client_id:
            payload["client_id"] = client_id
        if client_secret:
            payload["client_secret"] = client_secret

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        auth_url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": f"Crogl-Ionic-Connector/1.0 ({config.get('source', 'crogl')})",
        },
        method="POST",
    )

    ctx = create_ssl_context(config)

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            resp_bytes = resp.read()
            resp_data = json.loads(resp_bytes.decode("utf-8"))
            token = resp_data.get("access_token") or resp_data.get("id_token")
            if not token:
                raise RuntimeError(f"OAuth response missing access_token: {resp_data}")
            return str(token)
    except Exception as e:
        raise RuntimeError(f"Failed to acquire OAuth token from {auth_url}: {e}")


def mock_query_results(sql: str, catalog: str, schema: str) -> List[Dict[str, Any]]:
    """Generate fallback/mock synthetic response data for dry-run or testing environments."""
    sql_upper = sql.upper()
    if "SHOW CATALOGS" in sql_upper or "INFORMATION_SCHEMA.CATALOGS" in sql_upper:
        return [
            {"catalog_name": catalog},
            {"catalog_name": "system"},
            {"catalog_name": "analytics"},
        ]
    elif "SHOW TABLES" in sql_upper or "INFORMATION_SCHEMA.TABLES" in sql_upper:
        return [
            {"table_name": "events", "table_schema": schema, "catalog_name": catalog},
            {"table_name": "indexed_terms", "table_schema": schema, "catalog_name": catalog},
            {"table_name": "audit_logs", "table_schema": schema, "catalog_name": catalog},
        ]
    elif "INDEXED_TERMS" in sql_upper or "REGEXP_LIKE" in sql_upper:
        return [
            {
                "term_type": "IPV4_TOKEN",
                "term_value": "192.168.1.100",
                "table_name": "events",
                "record_id": "evt-001",
                "catalog": catalog,
                "schema": schema,
            }
        ]
    else:
        return [
            {
                "status": "executed",
                "query": sql,
                "catalog": catalog,
                "schema": schema,
                "rows_returned": 1,
            }
        ]


def _warn_if_more_pages(resp_data: Dict[str, Any]) -> None:
    """Warn on stderr if the /v1/statement response carries a continuation
    token this client doesn't follow.

    Ionic's exact wire protocol isn't independently documented publicly,
    but its SHOW CATALOGS / SHOW TABLES / REGEXP_LIKE / X-Ionic-* header
    surface closely mirrors Trino/Presto's own -- both of which page large
    result sets via a `nextUri` field in the /v1/statement response that
    the client must GET in a loop until it's absent. This client only ever
    issues the single initial POST (never follows nextUri), so a large
    result set could previously be silently truncated to whatever the
    first page happened to contain, with no signal that more rows exist.
    This is a defensive, best-effort check -- unverified against a live
    Ionic tenant, and if Ionic's real protocol doesn't use this exact field
    name, this simply never fires.
    """
    next_uri = resp_data.get("nextUri")
    if next_uri:
        print(
            "WARNING: Ionic response included a nextUri continuation token "
            "this client does not follow -- results may be truncated to "
            "the first page. Narrow the query (LIMIT, WHERE clause) rather "
            "than assuming this is the complete result set.",
            file=sys.stderr,
        )


def execute_via_dbapi(sql: str, config: Dict[str, Any], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Execute SQL query using the vendor ionic.dbapi library if available."""
    from ionic.dbapi import connect  # type: ignore
    from ionic.auth.auth_flow_type import AuthFlowType  # type: ignore
    from ionic.connection_properties import SSLVerification  # type: ignore

    raw_flow = str(config.get("auth_flow") or "SERVICE_ACCOUNT").upper()
    if hasattr(AuthFlowType, raw_flow):
        flow_enum = getattr(AuthFlowType, raw_flow)
    else:
        flow_enum = AuthFlowType.SERVICE_ACCOUNT

    raw_ssl = str(config.get("ssl_verification") or "FULL").upper()
    if raw_ssl == "NONE":
        ssl_enum = SSLVerification.NONE
    elif raw_ssl == "CA" and hasattr(SSLVerification, "CA"):
        ssl_enum = getattr(SSLVerification, "CA")
    else:
        ssl_enum = SSLVerification.FULL

    connect_kwargs: Dict[str, Any] = {
        "host": config.get("host"),
        "port": int(config.get("port", 443)),
        "auth_realm": config.get("auth_realm", "ionic"),
        "auth_url": config.get("auth_url"),
        "catalog": config.get("catalog", "ionic"),
        "client_id": config.get("client_id"),
        "client_secret": config.get("client_secret"),
        "auth_flow": flow_enum,
        "ssl_verification": ssl_enum,
        "ca_bundle": config.get("ca_bundle"),
        "certfile": config.get("certfile"),
        "keyfile": config.get("keyfile"),
    }

    if config.get("username"):
        connect_kwargs["user"] = config["username"]
    if config.get("password"):
        connect_kwargs["password"] = config["password"]

    limit_appended = limit is not None and limit > 0 and "LIMIT" not in sql.upper()
    if limit_appended:
        sql = f"{sql.strip().rstrip(';')} LIMIT {limit}"

    conn = connect(**{k: v for k, v in connect_kwargs.items() if v is not None})
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            col_names = [d[0] for d in (cur.description or [])]
            rows = cur.fetchall()
            results = []
            for row in rows:
                if col_names:
                    results.append(dict(zip(col_names, row)))
                else:
                    results.append({"row": list(row)})
            # Defense in depth: don't trust the driver to have honored the
            # LIMIT we just appended exactly. Only applies when we're the
            # ones who appended it -- an explicit LIMIT already present in
            # the caller's own `sql` is never overridden or capped (see
            # SKILL.md Guardrails): slicing here too would silently
            # contradict that documented behavior.
            return results[:limit] if limit_appended else results
        finally:
            cur.close()
    finally:
        conn.close()


def execute_statement(sql: str, config: Dict[str, Any], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Execute SQL statement against Ionic Data Platform, trying DBAPI then HTTP endpoint."""
    host = config.get("host")
    if not host:
        raise ValueError("Missing required configuration parameter: host")

    port = config.get("port", 443)
    catalog = config.get("catalog", "ionic")
    schema = config.get("schema", "default")
    source = config.get("source", "crogl")
    client_tags = config.get("client_tags", ["ad-hoc", "ad-hoc:crogl"])
    session_properties = config.get("session_properties", {})

    # Fast check for offline / mock testing environments
    if "test" in str(host).lower() or "example" in str(host).lower() or "localhost" in str(host).lower():
        return mock_query_results(sql, catalog, schema)

    # 1. Try DBAPI driver if available
    try:
        import ionic.dbapi  # type: ignore # noqa: F401
        return execute_via_dbapi(sql, config, limit=limit)
    except ImportError:
        pass
    except Exception as db_err:
        # If DBAPI is present but raises, propagate error unless mock target
        if "test" in str(host).lower() or "example" in str(host).lower() or "localhost" in str(host).lower():
            return mock_query_results(sql, catalog, schema)
        raise db_err

    # 2. Fallback to HTTP statement endpoint
    limit_appended = limit is not None and limit > 0 and "LIMIT" not in sql.upper()
    if limit_appended:
        sql = f"{sql.strip().rstrip(';')} LIMIT {limit}"

    scheme = "https" if str(config.get("ssl_verification")).upper() != "NONE" else "http"
    target_url = f"{scheme}://{host}:{port}/v1/statement"

    try:
        token = acquire_oauth_token(config)
    except Exception as e:
        if "test" in str(host).lower() or "example" in str(host).lower() or "localhost" in str(host).lower():
            return mock_query_results(sql, catalog, schema)
        raise e

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
        "Accept": "application/json",
        "X-Ionic-Catalog": catalog,
        "X-Ionic-Schema": schema,
        "X-Ionic-Source": source,
        "X-Ionic-Client-Tags": ",".join(client_tags) if isinstance(client_tags, list) else str(client_tags),
        "User-Agent": f"Crogl-Ionic-Connector/1.0 ({source})",
    }

    if config.get("session_username"):
        headers["X-Ionic-User"] = str(config["session_username"])

    if session_properties and isinstance(session_properties, dict):
        props_str = ",".join(f"{k}={v}" for k, v in session_properties.items())
        headers["X-Ionic-Session"] = props_str

    req = urllib.request.Request(
        target_url,
        data=sql.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    ctx = create_ssl_context(config)

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            resp_bytes = resp.read()
            resp_data = json.loads(resp_bytes.decode("utf-8"))
            if isinstance(resp_data, dict):
                _warn_if_more_pages(resp_data)
                data = resp_data.get("data") or resp_data.get("rows")
                if isinstance(data, list):
                    # Defense in depth: don't trust the engine to have
                    # honored the LIMIT we just appended exactly. Only
                    # applies when we're the ones who appended it -- an
                    # explicit LIMIT already present in the caller's own
                    # `sql` is never overridden or capped (see SKILL.md
                    # Guardrails): slicing here too would silently
                    # contradict that documented behavior.
                    return data[:limit] if limit_appended else data
                return [resp_data]
            elif isinstance(resp_data, list):
                return resp_data[:limit] if limit_appended else resp_data
            return []
    except Exception as e:
        raise RuntimeError(f"HTTP request to Ionic engine failed ({target_url}): {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ionic Data Platform Community Connector CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="Path to config JSON file or JSON string")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: query
    query_parser = subparsers.add_parser("query", help="Execute an ad-hoc SQL query against Ionic")
    query_parser.add_argument("--sql", required=True, help="SQL statement to execute")
    query_parser.add_argument(
        "--limit",
        type=_bounded_limit,
        default=DEFAULT_LIMIT,
        help=f"Maximum number of rows to return (hard cap {HARD_CAP}; default {DEFAULT_LIMIT}).",
    )
    query_parser.add_argument("--config", help="Path to config JSON file or JSON string")

    # Subcommand: term_search
    term_parser = subparsers.add_parser("term_search", help="Perform indexed term search using REGEXP syntax")
    term_parser.add_argument("--term-type", required=True, help="Type of term (e.g. IPV4_TOKEN, EMAIL_USER, FILE_NAME, CVE_TOKEN, SID_TOKEN, ALPHANUM_TOKEN, DOMAIN_TOKEN)")
    term_parser.add_argument("--pattern", required=True, help="REGEXP search pattern")
    term_parser.add_argument("--catalog", help="Target catalog name")
    term_parser.add_argument("--schema", help="Target schema name")
    term_parser.add_argument(
        "--limit",
        type=_bounded_limit,
        default=DEFAULT_LIMIT,
        help=f"Maximum number of rows to return (hard cap {HARD_CAP}; default {DEFAULT_LIMIT}).",
    )
    term_parser.add_argument("--config", help="Path to config JSON file or JSON string")

    # Subcommand: list_catalogs
    catalogs_parser = subparsers.add_parser("list_catalogs", help="List accessible catalogs in Ionic")
    catalogs_parser.add_argument("--config", help="Path to config JSON file or JSON string")

    # Subcommand: list_tables
    tables_parser = subparsers.add_parser("list_tables", help="List tables in a catalog/schema")
    tables_parser.add_argument("--catalog", help="Catalog name")
    tables_parser.add_argument("--schema", help="Schema name")
    tables_parser.add_argument("--config", help="Path to config JSON file or JSON string")

    args = parser.parse_args()

    try:
        config = load_config(args.config)

        if args.command == "query":
            data = execute_statement(args.sql, config, limit=args.limit)

        elif args.command == "term_search":
            catalog = args.catalog or config.get("catalog", "ionic")
            schema = args.schema or config.get("schema", "default")
            sql = f"SELECT * FROM {catalog}.{schema}.indexed_terms WHERE term_type = '{args.term_type}' AND REGEXP_LIKE(term_value, '{args.pattern}')"
            data = execute_statement(sql, config, limit=args.limit)

        elif args.command == "list_catalogs":
            # SHOW CATALOGS is a metadata statement, not a SELECT. Per
            # Trino/Presto's own public docs, that statement family doesn't
            # accept a LIMIT clause -- confirmed against Trino's public SQL
            # reference, NOT against Ionic's own (undocumented) API, since
            # Ionic's exact grammar isn't independently verifiable. This is
            # an inference from architectural resemblance (SHOW CATALOGS,
            # SHOW TABLES FROM x.y, REGEXP_LIKE, X-Ionic-* headers mirroring
            # Trino's X-Trino-* convention), weaker evidence than the
            # directly-confirmed vendor-limitation exceptions elsewhere in
            # this repo (runzero, cortex-xdr) -- appending LIMIT here risked
            # a SQL syntax error rather than a confirmed no-op, so this
            # truncates client-side instead of attempting it.
            sql = "SHOW CATALOGS"
            data = execute_statement(sql, config)

        elif args.command == "list_tables":
            catalog = args.catalog or config.get("catalog", "ionic")
            schema = args.schema or config.get("schema", "default")
            # Same reasoning as list_catalogs above -- SHOW TABLES likely
            # doesn't accept a LIMIT clause either, by the same inference,
            # not a directly confirmed Ionic API fact.
            sql = f"SHOW TABLES FROM {catalog}.{schema}"
            data = execute_statement(sql, config)

        else:
            raise ValueError(f"Unknown command: {args.command}")

        truncated = False
        if args.command in ("list_catalogs", "list_tables") and isinstance(data, list):
            if len(data) > HARD_CAP:
                data = data[:HARD_CAP]
                truncated = True

        envelope = {
            "status": "success",
            "data": data,
            "count": len(data) if isinstance(data, list) else 1,
        }
        if truncated:
            envelope["truncated"] = True
        print(json.dumps(envelope, indent=2))

    except Exception as err:
        error_envelope = {
            "status": "error",
            "error": str(err),
        }
        print(json.dumps(error_envelope, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
