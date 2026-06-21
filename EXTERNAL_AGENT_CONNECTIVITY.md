# External Agent Connectivity & Workflow

This document defines how Cortex AI connects to external agents and the workflow for agent-to-agent (A2A) collaboration.

## 1. Connecting External Agents

External agents can connect to Cortex AI by registering themselves via the API. Once registered, they appear in the Agent Registry and can be delegated tasks by the Orchestrator or other specialist agents.

### Registration Workflow
1. **Discovery**: External systems can call `GET /.well-known/agent.json` to discover Cortex's capabilities and endpoints.
2. **Registration**: The external agent POSTs its profile to `/api/agents/external/register`.
   ```json
   {
     "agent_id": "my-external-agent",
     "display_name": "External Specialist",
     "url": "https://agent-api.com/webhook",
     "description": "Expert in specific external tasks."
   }
   ```
3. **Verification**: Cortex records the agent and provides it with a `task_submission_url`.

## 2. Task Delegation Workflow

Cortex uses a robust retry-enabled dispatcher for outbound task delegation.

1. **Task Submission**: A task is submitted to an agent via `POST /api/agents/{agent_id}/tasks`.
2. **Dispatching**:
   - For **Internal Agents**: The task is queued for local processing.
   - For **External Agents**: The `WebhookDispatcher` attempts to POST the task to the agent's registered URL.
3. **Retry Logic**: If the external agent is offline, the dispatcher uses exponential backoff (up to 5 retries).
4. **Execution**: The external agent processes the task.
5. **Callback**: The external agent POSTs the result back to `POST /api/tasks/{task_id}/result`.

## 3. The 11-Layer Knowledge Pipeline

Cortex processes data through 11 sequential layers to ensure knowledge is clean, verified, and agent-ready.

1.  **INGESTION**: Pull data from Slack, Notion, GitHub, and Local Files.
2.  **NORMALIZATION**: Strip noise and standardize formatting.
3.  **CHUNKING**: Semantic splitting of text.
4.  **DISTILLATION**: AI-powered extraction of operational rules.
5.  **DEDUPLICATION**: Similarity-based merging.
6.  **PRIVACY SCAN**: PII and secret detection/redaction.
7.  **ATOMIC SEGMENTATION**: Decomposing claims into atomic units.
8.  **CONFLICT RESOLUTION**: Flagging and resolving contradictions.
9.  **SYNTHESIS**: Compiling approved units into `SKILL.md` documents.
10. **MEMORY INDEXING**: Writing to the 8-tier SQLite memory architecture.
11. **AGENT ORCHESTRATION**: Injecting updated context and tools into agents.

## 4. Connectivity Logging

All connectivity events are logged in `connection.log`, including:
- Data source connection success/failure.
- External agent registration.
- Task dispatch events and results.
