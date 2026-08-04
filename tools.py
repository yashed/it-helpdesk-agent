"""Tool definitions exposed to the LangGraph IT helpdesk agent.

Each tool wraps a call into ``clients/`` and serializes results as plain strings
suitable for the LLM. Errors from the clients are returned as readable strings,
not raised, so the agent can decide what to do (typically: escalate).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from langchain_core.tools import tool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from clients import employees as employees_client
from clients import escalations as escalations_client
from clients import passwords as passwords_client
from clients import policies as policies_client
from clients import software as software_client
from clients import system_status as status_client
from clients import tickets as tickets_client
from config import Config
from identity import AGENT_IDENTITY

log = logging.getLogger("it-helpdesk.tools")


class DynamicHeaders(dict):
    def __init__(self, get_token_func):
        super().__init__()
        self.get_token_func = get_token_func

    def _get_auth_header(self) -> str:
        token = self.get_token_func()
        log.info("AGENTID_HEADERS_ACCESSED token=%s...", token[:15] if token else "none")
        return f"Bearer {token}"

    def items(self):
        auth = self._get_auth_header()
        return [("Authorization", auth)]

    def __getitem__(self, key):
        if key == "Authorization":
            return self._get_auth_header()
        raise KeyError(key)

    def get(self, key, default=None):
        if key == "Authorization":
            return self._get_auth_header()
        return default

    def __contains__(self, key):
        return key == "Authorization"

    def keys(self):
        return ["Authorization"]

    def __iter__(self):
        return iter(["Authorization"])

    def __len__(self):
        return 1

    def copy(self):
        return self


def make_sync_tool(async_tool):
    def sync_run(*args, **kwargs):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, async_tool.coroutine(*args, **kwargs))
                return future.result()
        else:
            return asyncio.run(async_tool.coroutine(*args, **kwargs))

    return StructuredTool(
        name=async_tool.name,
        description=async_tool.description,
        args_schema=async_tool.args_schema,
        func=sync_run,
        coroutine=async_tool.coroutine,
        handle_tool_error=async_tool.handle_tool_error,
        response_format=async_tool.response_format,
    )


def _load_mcp_tools() -> list[Any]:
    mcp_tools = []
    mcp_server_url = (AGENT_IDENTITY.test_mcp_url or os.environ.get("TEST_MCP_URL", "")).strip()
    if not mcp_server_url:
        log.info("AGENTID_MCP_CONNECT_SKIP reason='TEST_MCP_URL environment variable is empty'")
        return mcp_tools

    access_token = ""
    if AGENT_IDENTITY.available:
        try:
            access_token = AGENT_IDENTITY.get_token()
            log.info("AGENTID_MCP_CONNECT_START url=%s token=%s...", mcp_server_url, access_token[:15])
        except Exception as exc:
            log.error("AGENTID_MCP_TOKEN_ERROR url=%s error=%s", mcp_server_url, exc)
            return mcp_tools
    else:
        log.warning("AGENTID_MCP_CONNECT_WARNING url=%s reason='AgentID identity is not available'", mcp_server_url)

    headers = DynamicHeaders(AGENT_IDENTITY.get_token) if AGENT_IDENTITY.available else {}

    server_configs: dict[str, dict[str, Any]] = {
        "mcp_server": {
            "url": mcp_server_url,
            "transport": "streamable_http",
            "headers": headers,
        }
    }

    async def _fetch():
        client = MultiServerMCPClient(server_configs)
        return await client.get_tools()

    try:
        raw_tools = asyncio.run(_fetch())
        mcp_tools = [make_sync_tool(t) for t in raw_tools]
        log.info("AGENTID_MCP_CONNECT_SUCCESS url=%s tools=%r", mcp_server_url, [t.name for t in mcp_tools])
    except Exception as exc:
        log.error("AGENTID_MCP_CONNECT_FAILED url=%s error=%s", mcp_server_url, exc)

    return mcp_tools


def build_tools(cfg: Config) -> list[Any]:
    """Build the tool set bound to the given instance config."""

    def _require_verified(employee_id: str) -> dict[str, Any] | None:
        """Return the employee record if verified, or None with an error string."""
        try:
            emp = employees_client.get(employee_id)
        except employees_client.EmployeeNotFound:
            return None
        return emp if emp.get("verified") else None

    @tool
    def lookup_employee(email: str) -> str:
        """Look up an employee by their email address.

        Returns the employee record (id, name, role, department, manager, admin status).
        Use this as the first step to identify who the requester is.
        """
        try:
            emp = employees_client.lookup_by_email(email)
            return json.dumps(emp)
        except employees_client.EmployeeNotFound as e:
            return f"ERROR: {e}"

    @tool
    def verify_identity(email: str, employee_id: str) -> str:
        """Verify an employee's identity by matching their email and employee ID.

        This MUST be called before any write action (password reset, access request,
        ticket creation). Returns the verified employee record.
        """
        try:
            emp = employees_client.verify(email, employee_id)
            return json.dumps(emp)
        except employees_client.EmployeeNotFound as e:
            return f"ERROR: {e}"

    @tool
    def get_open_tickets(employee_id: str) -> str:
        """List an employee's tickets (open, in-progress, and recent resolved).

        Use this to check for existing tickets before creating new ones
        (duplicate prevention policy).
        """
        rows = tickets_client.get_by_employee(employee_id)
        return json.dumps(rows[: cfg.max_tickets_per_query])

    @tool
    def create_ticket(
        employee_id: str,
        subject: str,
        category: str,
        priority: str,
        description: str,
    ) -> str:
        """Create a new IT support ticket.

        Categories: network, software, hardware, access, email, security, other.
        Priorities: P1 (critical), P2 (high), P3 (medium), P4 (low).

        Requires identity to be verified first via verify_identity.
        Before creating, check for duplicate tickets and system status.
        Do not create a ticket if the issue matches a known outage.
        """
        if _require_verified(employee_id) is None:
            return "ERROR: Identity has not been verified. Call verify_identity first."
        dupes = tickets_client.find_duplicates(employee_id, category)
        if dupes:
            return json.dumps({
                "warning": "Potential duplicate ticket(s) found. Consider updating the existing ticket instead.",
                "existing_tickets": dupes,
            })
        ticket = tickets_client.create(
            employee_id=employee_id,
            subject=subject,
            category=category,
            priority=priority,
            description=description,
        )
        return json.dumps(ticket)

    @tool
    def reset_password(employee_id: str) -> str:
        """Reset an employee's password.

        Requires identity to be verified first via verify_identity.
        Admin accounts CANNOT be reset — they must be escalated to L2 Security.
        On success, a temporary password is sent to the employee's registered email.
        """
        emp = _require_verified(employee_id)
        if emp is None:
            return "ERROR: Identity has not been verified. Call verify_identity first."
        try:
            receipt = passwords_client.reset(
                employee_id=employee_id,
                is_admin=emp["is_admin"],
                verified=True,
            )
            return json.dumps(receipt)
        except passwords_client.AdminResetBlocked as e:
            return f"ERROR: {e} Use escalate_to_l2 to forward this request."

    @tool
    def request_software_access(employee_id: str, software_name: str) -> str:
        """Request access to a software tool for an employee.

        Requires identity to be verified first via verify_identity.
        Access is granted based on department eligibility. Some tools require
        manager approval. Requests outside the employee's department allowlist
        are denied.
        """
        emp = _require_verified(employee_id)
        if emp is None:
            return "ERROR: Identity has not been verified. Call verify_identity first."
        try:
            result = software_client.request_access(
                employee_id=employee_id,
                department=emp["department"],
                manager_id=emp["manager_id"],
                software_name=software_name,
            )
            return json.dumps(result)
        except software_client.SoftwareNotFound as e:
            return f"ERROR: {e}"
        except software_client.DepartmentNotAllowed as e:
            return f"ERROR: {e}"
        except software_client.ApprovalRequired as e:
            return f"PENDING: {e}"

    @tool
    def check_system_status(service_query: str = "") -> str:
        """Check the current status of IT systems and services.

        Pass a service name to search (e.g. 'email', 'vpn') or leave empty
        to get all non-operational services. Always check this before creating
        tickets for connectivity or service issues.
        """
        if service_query.strip():
            results = status_client.check_service(service_query)
        else:
            results = status_client.get_degraded()
        if not results:
            return "All systems operational — no issues found matching your query."
        return json.dumps(results)

    @tool
    def search_it_policy(query: str) -> str:
        """Search the IT policy knowledge base for relevant policies.

        Use this when you need to cite a policy before taking an action
        or when answering policy-related questions.
        """
        docs = policies_client.search(query=query, limit=3)
        return json.dumps(docs)

    @tool
    def escalate_to_l2(employee_id: str, summary: str, priority: str, category: str) -> str:
        """Escalate a case to L2 support.

        Use this for: admin account issues, security incidents, infrastructure access,
        complex problems the L1 agent cannot resolve, or any situation requiring
        elevated privileges.

        Categories: security, infrastructure, admin_account, complex_issue.
        """
        ticket = escalations_client.create(
            employee_id=employee_id,
            summary=summary,
            priority=priority,
            category=category,
        )
        return json.dumps(ticket)

    base_tools = [
        lookup_employee,
        verify_identity,
        get_open_tickets,
        create_ticket,
        reset_password,
        request_software_access,
        check_system_status,
        search_it_policy,
        escalate_to_l2,
    ]
    return base_tools + _load_mcp_tools()
