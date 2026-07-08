# IT Helpdesk Agent - Deployment Guide

## Overview

The IT Helpdesk Agent is an AI-powered L1 IT support assistant that helps employees with password resets, software access requests, ticket management, and system status inquiries. Built with LangGraph and FastAPI, this agent enforces identity verification, respects admin account restrictions, detects duplicate tickets, and escalates complex issues to L2 support.

## Prerequisites

Before deploying this agent, ensure you have:

### Required API Keys

- **OpenAI API Key**: For GPT-powered conversations

## 1. Deploy in Agent Manager

### Step 1: Access Agent Manager

1. Navigate to the **Default** project
2. Select **Platform-Hosted Agent** Card
3. Pick **Source Code** as the source type of the agent

### Step 2: Configure Agent Details

Fill in the agent creation form with these exact values:

| Field                 | Value                                                        |
| --------------------- | ------------------------------------------------------------ |
| **Display Name**      | `IT Helpdesk Agent`                                          |
| **Description**       | `AI-powered IT helpdesk agent for employee technical support` |
| **GitHub Repository** | `https://github.com/wso2/agent-manager`                      |
| **Branch**            | `main`                                                       |
| **App Path**          | `/samples/it-helpdesk-agent`                                  |
| **Language**          | `Python`                                                     |
| **Language Version**  | `3.11`                                                       |
| **Start Command**     | `python main.py`                                             |

### Step 3: Select Agent Interface

- Choose **"Chat Agent"** as the agent interface type

### Step 4: Configure Environment Variables

Add the following environment variables in the create form:

```env
OPENAI_API_KEY=<your-openai-api-key>
```

### Step 5: Deploy the Agent

1. Review all configuration details
2. Click **"Deploy"**
3. Wait for the build to complete (typically 6-10 minutes)

## 2. Invoking the Agent

### Step 1: Navigate to Chat Interface

Click on the **"Try It"** section on the left navigation.

### Step 2: Test Sample Interactions

Try these sample queries in the chat interface. Each query exercises a different tool chain — visible in traces.

**Password reset (happy path — multi-step tool chain):**

```text
Hi, I am alice.chen@acmecorp.com, employee ID E-1001. I forgot my password and need a reset.
```

**Password reset blocked (admin account → escalation):**

```text
Hi, david.kim@acmecorp.com here, employee ID E-1004. I need my password reset urgently.
```

**Known outage detection (agent checks status, no ticket created):**

```text
Hi, I am bob.martinez@acmecorp.com, employee ID E-1002. My email is not syncing — is something wrong?
```

## 3. Traces

1. Navigate to **Observability** > **Traces** on the left navigation
2. Click on a trace to view the tool call chain for each interaction

## 4. Evaluators

Evaluators let you  assess agent behavior across traces. The **Sequence Adherence** evaluator is ideal for this agent — it verifies that tools were called in the correct order (e.g., identity verification before password reset).

### Step 1: Create an Eval Monitor

1. Go to the agent and click **Monitors** under **Evaluation** in the left navigation
2. Click **Create Monitor**
3. Set the title as `Sequence Eval Monitor`

### Step 2: Configure Trace Selection

1. Keep **Past Traces** selected
2. Adjust the time range if needed to include your test traces
3. Click **Next**

### Step 3: Add the Sequence Adherence Evaluator

1. Select the **Sequence Adherence** evaluator
2. Set the expected sequence: `lookup_employee`, `verify_identity`
3. Click **Add Evaluator**

### Step 4: Run the Monitor

1. Wait for the monitor to run against the selected traces
2. Review the results — traces where the agent followed the correct tool order will score 100%, while traces that skipped steps (e.g., missing `verify_identity`) will score lower

## 5. LLM Providers & Guardrails

This demonstrates how platform admins can govern agent behavior without changing agent code, using the **Prompt Decorator** guardrail.

**Without the guardrail**, the agent happily answers off-topic requests like:

```text
Hi, can you help me write an email to my manager explaining why I need a raise?
```

Follow these steps to add a guardrail that restricts the agent to IT support queries only:

### Step 1: Set the Environment Variable

1. Go to the agent's **Deploy** page
2. Click **Configure and Deploy**
3. Add the environment variable `USE_LLM_PROVIDER=true`
4. Click **Deploy** and wait for the deployment to complete

### Step 2: Create an LLM Service Provider

1. Navigate to the organization level and select **LLM Service Providers** from the left navigation
2. Click **Add Service Provider**
3. Set the name as `openai llm provider` and select **OpenAI** as the provider template
4. Enter your OpenAI API key and click **Add Provider**

### Step 3: Add the LLM Provider to the Agent

1. Go to the agent and click **Configure** from the left navigation
2. Click **Add LLM Provider**
3. Set the name as `openai gpt` and select the created LLM service provider under the service provider list

### Step 4: Add the Prompt Decorator Guardrail

1. Click **Add Guardrail** and select **Prompt Decorator**
2. Under **messages**, click **+ Add Item** and set:
   - **role**: `system`
   - **content**:
     ```text
     You must ONLY respond to IT support related queries such as password resets,
     software access, ticket management, system status, and IT policies. For any
     non-IT requests, politely decline and redirect the user to the appropriate department.
     ```
3. Leave **Json Path** empty and **Append** off
4. Click **Add**

### Step 5: Configure Environment Variable References

1. In the LLM provider's **Environment Variables References**, rename the variables:
   - `Base URL of the LLM provider` → `LLM_PROVIDER_URL`
   - `API Key for authentication` → `LLM_PROVIDER_KEY`
2. Click **Save**
3. Wait until the component is deployed with the new configurations

### Step 6: Test the Guardrail

Try the same off-topic query again in the **Try It** chat interface:

```text
Hi, can you help me write an email to my manager explaining why I need a raise?
```

The agent now declines the off-topic request — the **Prompt Decorator** guardrail prepends a system message that restricts the agent to IT support queries only, without any changes to the agent's code.

## 6. Testing AgentID Credential Injection (Gateway Binding)

This agent also demonstrates the platform's **AgentID** feature: as a platform-hosted (internal) agent, it automatically receives its own OAuth2 identity — a `client_id`, `client_secret`, token endpoint, and scopes — injected as environment variables, with no configuration needed on your part. `identity.py` reads these, mints a token, and logs/exposes enough to verify the whole flow end to end. No new environment variables need to be added for this — it works automatically for every internal agent.

### What to check after deploying

1. **Injection happened at all.** Check the pod logs (Observability → Traces won't show this — use the platform's log viewer, or `kubectl logs` if you have cluster access) for a line at startup:

   ```text
   AGENTID_STATUS available=true client_id=<some-id> token_endpoint=http://amp-thunder-<org>-<env>-service...:8090/oauth2/token scopes='amp:mcp:invoke'
   ```

   If you instead see `available=false missing_env_vars=[...]`, injection did not happen — check that this agent is provisioned as **internal** (Platform-Hosted), not external, and that its AgentID identity has finished provisioning (`GET .../identities` from the AMS API should show `status: completed` for this environment).

2. **A real token can be minted.** Immediately after the status line, look for:

   ```text
   AGENTID_TOKEN_MINTED client_id=<id> requested_scope='amp:mcp:invoke' granted_scope='...' expires_in=3600 mint_count=1
   ```

   If you instead see `AGENTID_TOKEN_MINT_FAILED`, the credentials were injected but the token endpoint call itself failed — check the error message; a common cause during local/dev testing is the env-Thunder instance not being reachable from where the agent is actually running.

3. **Or just call the agent's own `/identity` endpoint** — this is the fastest way to check state without digging through logs, and it never needs a chat interaction:

   ```bash
   curl https://<this-agent's-endpoint>/identity
   ```

   ```json
   {
     "available": true,
     "client_id": "a1b2c3d4-...",
     "token_endpoint": "http://amp-thunder-default-dev-service.amp-thunder-default-dev.svc.cluster.local:8090/oauth2/token",
     "scopes_requested": "amp:mcp:invoke",
     "token_status": "valid",
     "mint_count": 1,
     "minted_at": 1751932800.12,
     "refresh_due_at": 1751936250.12,
     "last_error": null
   }
   ```

   Add `?force=true` to force a brand-new mint right now instead of waiting for the natural refresh window — useful right after a rotate/revoke to see the new state immediately rather than waiting.

### Verifying credentials differ per environment

1. Deploy this agent to your lowest environment, note its `client_id` from `/identity`.
2. Promote it to the next environment.
3. Call `/identity` on **both** running instances (the dev one and the newly-promoted one).
4. **Expected: different `client_id` values.** Each environment has its own Thunder identity — the promoted environment must never show the source environment's credentials. If both show the same `client_id`, that's a real bug in the injection/promotion logic, not expected behavior — worth reporting.

### Verifying rotation propagates to the running pod

1. Note the current `client_id`/`mint_count`/`token_status` from `/identity`.
2. Trigger a secret regenerate for this agent+environment (via the AMS API: `POST .../agents/{agent}/identities` with `{"environment": "<env>"}`).
3. Watch for the pod to restart (this is expected — rotation forces a rollout so the new pod starts with the fresh secret).
4. Once the new pod is up, call `/identity` again:
   - `client_id` should be **unchanged** (rotation only replaces the secret, not the identity).
   - `mint_count` resets to `1` (it's a fresh process).
   - Check the logs for a fresh `AGENTID_TOKEN_MINTED` line — this confirms the new pod actually got the *rotated* secret and could mint successfully with it, not a stale one.

### On scopes

`scopes_requested` will currently always show `amp:mcp:invoke` — this is a fixed placeholder value the platform injects until the MCP-permission phase (assigning specific MCP-tool permissions to an agent) is implemented. There is currently no way to make this value actually change, since nothing yet computes a real, agent-specific scope list — this field exists so the injection contract is ready for that phase without needing agent code changes later. If `granted_scope` in the `AGENTID_TOKEN_MINTED` log line ever differs from `requested_scope`, that's expected too — see the `AGENTID_SCOPE_MISMATCH` log line's explanation: Thunder only grants what the agent's role assignments in Thunder actually allow, regardless of what was requested.
