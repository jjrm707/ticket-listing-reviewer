import ast
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability
from ticket_reviewer.connectors.seatgeek import SeatGeekConnector
from ticket_reviewer.connectors.stubhub import StubHubConnector
from ticket_reviewer.connectors.ticketmaster import TicketmasterConnector
from ticket_reviewer.main import create_app
from ticket_reviewer.connectors import seatgeek, stubhub, ticketmaster
from ticket_reviewer.services.alerts import NtfyPublisher, PushMessage


def _audit_outbound_http_calls(package: Path) -> list[tuple[str, str]]:
    calls = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "request",
                "send",
            }:
                continue
            owner = node.func.value
            owner_name = ""
            if isinstance(owner, ast.Attribute):
                owner_name = owner.attr
            elif isinstance(owner, ast.Name):
                owner_name = owner.id
            if "client" in owner_name.casefold() or owner_name == "httpx":
                calls.append((path.relative_to(package).as_posix(), node.func.attr))
    return calls


def _audit_exposed_names(package: Path, relatives: tuple[str, ...]) -> set[str]:
    exposed = set()
    for relative in relatives:
        for path in (package / relative).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    exposed.add(node.name.casefold())
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        exposed.add(node.func.id.casefold())
                    elif isinstance(node.func, ast.Attribute):
                        exposed.add(node.func.attr.casefold())
    return exposed


def _audit_network_wrapper_violations(package: Path) -> list[tuple[str, str]]:
    violations = []

    def reviewed_constant(argument):
        if isinstance(argument, ast.Name):
            return argument.id == "_SEARCH_URL"
        return (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Attribute)
            and argument.func.attr == "format"
            and isinstance(argument.func.value, ast.Name)
            and argument.func.value.id == "_DETAIL_URL"
        )

    class WrapperVisitor(ast.NodeVisitor):
        def __init__(self, relative):
            self.relative = relative
            self.functions = []
            self.reviewed_variables = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.reviewed_variables.append(set())
            self.generic_visit(node)
            self.reviewed_variables.pop()
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def reviewed_expression(self, node):
            return reviewed_constant(node) or (
                isinstance(node, ast.Name)
                and bool(self.reviewed_variables)
                and node.id in self.reviewed_variables[-1]
            )

        def update_target(self, target, value):
            if not self.reviewed_variables or not isinstance(target, ast.Name):
                return
            self.reviewed_variables[-1].discard(target.id)
            if self.reviewed_expression(value):
                self.reviewed_variables[-1].add(target.id)

        def visit_Assign(self, node):
            for target in node.targets:
                self.update_target(target, node.value)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if node.value is not None:
                self.update_target(node.target, node.value)
            self.generic_visit(node)

        def visit_NamedExpr(self, node):
            self.update_target(node.target, node.value)
            self.generic_visit(node)

        def visit_AugAssign(self, node):
            if self.reviewed_variables and isinstance(node.target, ast.Name):
                self.reviewed_variables[-1].discard(node.target.id)
            self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute):
                wrapper = node.func.attr
                valid = False
                if wrapper in {"_get_json", "_catalog_json"}:
                    valid = bool(node.args) and (
                        self.reviewed_expression(node.args[0])
                    )
                elif wrapper == "_catalog_json_with_token":
                    valid = (
                        bool(node.args)
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "url"
                        and self.functions[-1:] == ["_catalog_json"]
                    )
                else:
                    self.generic_visit(node)
                    return
                if not valid:
                    violations.append((self.relative, wrapper))
            self.generic_visit(node)

    for path in package.rglob("*.py"):
        relative = path.relative_to(package).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        WrapperVisitor(relative).visit(tree)
    return sorted(violations)


def _audit_transaction_names(
    package: Path, relatives: tuple[str, ...]
) -> list[tuple[str, str]]:
    suspicious = []

    def is_transaction(name):
        separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
        folded = separated.casefold()
        if folded == "order_by":
            return False
        tokens = set(re.findall(r"[a-z]+", folded))
        if tokens & {
            "purchase",
            "reserve",
            "cart",
            "order",
            "transfer",
            "reprice",
            "sell",
            "relist",
            "relisting",
            "relisted",
        }:
            return True
        normalized = "".join(re.findall(r"[a-z]+", folded))
        composite_stems = {
            "purchase",
            "reserve",
            "cart",
            "order",
            "transfer",
            "reprice",
            "relist",
        }
        if any(
            normalized.startswith(stem) or normalized.endswith(stem)
            for stem in composite_stems
        ):
            return True
        return {"upload", "listing"} <= tokens or {"write", "inventory"} <= tokens

    for relative in relatives:
        for path in (package / relative).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.append(node.name)
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        names.append(node.func.id)
                    elif isinstance(node.func, ast.Attribute):
                        names.append(node.func.attr)
            suspicious.extend(
                (path.relative_to(package).as_posix(), name)
                for name in names
                if is_transaction(name)
            )
    return sorted(set(suspicious))


@pytest.mark.parametrize(
    "path",
    [
        "/.env",
        "/%2eenv",
        "/.git/config",
        "/data/ticket_reviewer.db",
        "/data/ticket_reviewer.db-wal",
        "/data/ticket_reviewer.db-shm",
        "/data/ticket_reviewer.db.lock",
        "/data/backups/ticket_reviewer.db",
        "/logs/app.log",
        "/screenshots/private.png",
        "/docs/superpowers/specs/private.md",
        "/ticket_reviewer/main.py",
        "/static/../templates/base.html",
        "/static/%2e%2e/templates/base.html",
        r"/static/..\templates\base.html",
        "/static/%5c..%5ctemplates%5cbase.html",
        r"/data\ticket_reviewer.db",
    ],
)
def test_private_or_workspace_paths_are_never_served(path):
    response = TestClient(create_app(Settings(_env_file=None)), base_url="http://127.0.0.1").get(path)
    assert response.status_code in {400, 404}


def test_static_mount_rejects_symlink_escape_when_supported(tmp_path):
    static_root = Path(__file__).parents[1] / "ticket_reviewer" / "web" / "static"
    outside = tmp_path / "private-sentinel.txt"
    outside.write_text("static-private-sentinel", encoding="utf-8")
    link = static_root / "task15-symlink-escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks is unavailable")
    try:
        response = TestClient(
            create_app(Settings(_env_file=None)), base_url="http://127.0.0.1"
        ).get("/static/task15-symlink-escape.txt")
        assert response.status_code in {400, 404}
        assert "static-private-sentinel" not in response.text
    finally:
        link.unlink(missing_ok=True)


def test_production_openapi_contains_no_internal_scan_or_transaction_route():
    schema = create_app(Settings(_env_file=None)).openapi()
    paths = " ".join(schema["paths"]).casefold()
    assert "/internal/scan" not in paths
    for forbidden in ("purchase", "reserve", "cart", "order", "transfer", "reprice", "sell"):
        assert forbidden not in paths


def test_production_connectors_advertise_read_only_public_capabilities():
    for connector_type in (TicketmasterConnector, SeatGeekConnector, StubHubConnector):
        assert connector_type.capabilities <= {Capability.EVENT_SEARCH, Capability.EVENT_PRICE}
        assert Capability.LISTING_DETAIL not in connector_type.capabilities


def test_runtime_marketplace_and_notification_requests_use_exact_allowlist():
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) == stubhub._TOKEN_URL:
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "read:events",
                },
                request=request,
            )
        if request.url.host == "ntfy.sh":
            return httpx.Response(200, json={"id": "synthetic-message-id"}, request=request)
        return httpx.Response(200, json={}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tm = TicketmasterConnector(
        Settings(_env_file=None, ticketmaster_api_key="synthetic-key"), client, sleep=lambda _: None
    )
    sg = SeatGeekConnector(
        Settings(_env_file=None, seatgeek_client_id="synthetic-client"),
        client,
        sleep=lambda _: None,
    )
    sh_settings = Settings(
        _env_file=None,
        stubhub_client_id="synthetic-client",
        stubhub_client_secret="synthetic-secret",
    )
    tokens = stubhub.StubHubTokenProvider(sh_settings, client, sleep=lambda _: None)
    sh = StubHubConnector(sh_settings, client, token_provider=tokens, sleep=lambda _: None)
    tm._get_json(ticketmaster._SEARCH_URL, {})
    tm._get_json(ticketmaster._DETAIL_URL.format(event_id="123"), {})
    sg._get_json(seatgeek._SEARCH_URL, {})
    sg._get_json(seatgeek._DETAIL_URL.format(event_id="123"), {})
    token = tokens.get_token(datetime(2026, 8, 9, tzinfo=timezone.utc))
    sh._catalog_json_with_token(stubhub._SEARCH_URL, {}, token)
    sh._catalog_json_with_token(stubhub._DETAIL_URL.format(event_id="123"), {}, token)
    publisher = NtfyPublisher("synthetic-topic-123456789", None, 10, client=client)
    publisher.publish(PushMessage("Synthetic", "Body", "high", ("ticket",), None))

    assert Counter((request.method, str(request.url).split("?")[0]) for request in requests) == Counter(
        {
            ("GET", ticketmaster._SEARCH_URL): 1,
            ("GET", ticketmaster._DETAIL_URL.format(event_id="123")): 1,
            ("GET", seatgeek._SEARCH_URL): 1,
            ("GET", seatgeek._DETAIL_URL.format(event_id="123")): 1,
            ("POST", stubhub._TOKEN_URL): 1,
            ("GET", stubhub._SEARCH_URL): 1,
            ("GET", stubhub._DETAIL_URL.format(event_id="123")): 1,
            ("POST", "https://ntfy.sh/synthetic-topic-123456789"): 1,
        }
    )


def test_ast_audit_finds_only_the_reviewed_outbound_http_call_sites():
    package = Path(__file__).parents[1] / "ticket_reviewer"
    assert Counter(_audit_outbound_http_calls(package)) == Counter(
        {
            ("connectors/ticketmaster.py", "get"): 1,
            ("connectors/seatgeek.py", "get"): 1,
            ("connectors/stubhub.py", "get"): 1,
            ("connectors/stubhub.py", "post"): 1,
            ("services/alerts.py", "post"): 1,
        }
    )
    assert _audit_network_wrapper_violations(package) == []


def test_ast_and_runtime_expose_no_transaction_behavior():
    forbidden = {
        "purchase",
        "reserve",
        "cart",
        "order",
        "transfer",
        "upload_listing",
        "sell",
        "relist",
        "reprice",
        "write_inventory",
    }
    package = Path(__file__).parents[1] / "ticket_reviewer"
    exposed = _audit_exposed_names(package, ("connectors", "services", "web"))
    assert forbidden.isdisjoint(exposed)
    assert _audit_transaction_names(package, ("connectors", "services", "web")) == []
    app = create_app(Settings(_env_file=None))
    assert all(
        forbidden.isdisjoint({part.casefold() for part in route.path.split("/") if part})
        for route in app.routes
        if hasattr(route, "path")
    )


def test_documented_endpoint_constants_include_ntfy_and_only_authorized_families():
    assert {
        ticketmaster._SEARCH_URL,
        ticketmaster._DETAIL_URL,
        seatgeek._SEARCH_URL,
        seatgeek._DETAIL_URL,
        stubhub._TOKEN_URL,
        stubhub._SEARCH_URL,
        stubhub._DETAIL_URL,
        "https://ntfy.sh/{topic}",
    } == {
        "https://app.ticketmaster.com/discovery/v2/events.json",
        "https://app.ticketmaster.com/discovery/v2/events/{event_id}.json",
        "https://api.seatgeek.com/2/events",
        "https://api.seatgeek.com/2/events/{event_id}",
        "https://account.stubhub.com/oauth2/token",
        "https://api.stubhub.net/catalog/events/search",
        "https://api.stubhub.net/catalog/events/{event_id}",
        "https://ntfy.sh/{topic}",
    }


def test_connectors_expose_no_marketplace_transaction_method():
    forbidden = {
        "purchase",
        "reserve",
        "cart",
        "order",
        "transfer",
        "upload_listing",
        "sell",
        "relist",
        "reprice",
        "write_inventory",
    }
    for connector_type in (TicketmasterConnector, SeatGeekConnector, StubHubConnector):
        assert forbidden.isdisjoint(dir(connector_type))


def test_run_script_has_one_fixed_loopback_listener_and_no_argument_forwarding():
    script = (Path(__file__).parents[1] / "scripts" / "run.ps1").read_text(encoding="utf-8")
    assert script.count("--host 127.0.0.1 --port 8765") == 1
    assert "$args" not in script


def test_http_audit_detects_existing_wrapper_called_with_dynamic_endpoint(tmp_path):
    module = tmp_path / "connectors" / "refresh.py"
    module.parent.mkdir()
    module.write_text(
        "def refresh(self):\n    return self._get_json('https://evil.example', {})\n",
        encoding="utf-8",
    )
    assert _audit_network_wrapper_violations(tmp_path) == [
        ("connectors/refresh.py", "_get_json")
    ]


def test_http_audit_invalidates_reviewed_variable_after_reassignment(tmp_path):
    module = tmp_path / "connectors" / "refresh.py"
    module.parent.mkdir()
    module.write_text(
        "def refresh(self):\n"
        "    url = _SEARCH_URL\n"
        "    url = 'https://evil.example'\n"
        "    return self._get_json(url, {})\n",
        encoding="utf-8",
    )
    assert _audit_network_wrapper_violations(tmp_path) == [
        ("connectors/refresh.py", "_get_json")
    ]


def test_transaction_audit_detects_invocation_hidden_in_benign_function(tmp_path):
    module = tmp_path / "web" / "refresh.py"
    module.parent.mkdir()
    module.write_text(
        "def refresh(marketplace):\n    return marketplace.submitOrder()\n",
        encoding="utf-8",
    )
    assert _audit_transaction_names(tmp_path, ("web",)) == [
        ("web/refresh.py", "submitOrder")
    ]


def test_transaction_audit_detects_relisting_without_banning_generic_list_names(tmp_path):
    module = tmp_path / "connectors" / "refresh.py"
    module.parent.mkdir()
    module.write_text(
        "def list_events(marketplace):\n    return marketplace.relistTickets()\n",
        encoding="utf-8",
    )
    assert _audit_transaction_names(tmp_path, ("connectors",)) == [
        ("connectors/refresh.py", "relistTickets")
    ]
