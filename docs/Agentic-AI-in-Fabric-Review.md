# Review: Agentic AI in Fabric Notebook

Oct 26, 2026

## Verdict

The notebook is mostly right, and its three-section split works: native agents, external agents, and MCP. It needs five fixes before it is complete:

1. **Add the data engineering agent (Project Osmos).** It is a native, autonomous Fabric agent, in preview, and it is missing.
2. **Rename "Fabric IQ ontology".** Fabric IQ isn't an agent; it is the shared business context that agents use. The agent there is the **ontology agent**. Your page text already says this.
3. **Mark the Copilots as assistants, not agents,** and add the missing ones: Data Warehouse, SQL database and Real-Time Intelligence.
4. **Merge the two Foundry pages.** "Azure AI Foundry" was renamed **Microsoft Foundry**, so pages 2 and 5 in External describe the same platform. Also note that its Fabric tool is retiring on 6 November 2026.
5. **Fill the empty Fabric MCP section** with the six MCP servers and endpoints Fabric offers.

Two smaller additions belong in External: Microsoft 365 Copilot and Teams, and the build-your-own tools (data agent SDK and AI functions).

One naming caution: strictly, only Microsoft Agent Framework is an agent *framework*. Everything else is a ready-made agent, an assistant, a platform, or a connector. Consider calling the notebook "Agentic AI in Fabric".

## Section 1: Native agents inside Fabric

| Your page | Verdict | Change |
| --- | --- | --- |
| 1. Fabric Data Agent | Correct | Keep. Generally available. |
| 2. Fabric Operations Agent | Correct | Keep. Generally available, part of Real-Time Intelligence. |
| 3. Copilot for Data Factory | Correct, but an assistant | Keep, under a "Copilot assistants" heading. |
| 4. Copilot for Power BI & ... | Correct, but an assistant | Keep under the same heading. |
| 5. Copilot for Data Science ... | Correct, but an assistant | Keep under the same heading; it covers notebooks for data engineering too. |
| 6. Fabric IQ ontology | Wrong label | Rename it "Ontology agent (Fabric IQ)". Fabric IQ is the context layer; the ontology agent is the agent. |
| (missing) Data engineering agent (Project Osmos) | Add | A native autonomous agent, in preview. It is the one most relevant to migration work. |
| (missing) Copilot for Data Warehouse, SQL database, Real-Time Intelligence | Add | They complete the Copilot set. |
| (missing) Build agent with AI | Optional | An assistant inside the data agent editor that drafts its instructions. Can be a sub-page of the data agent page. |

Suggested order: the four agents first (data agent, operations agent, data engineering agent, ontology agent), then the Copilot assistants.

## Section 2: External agents that use Fabric

| Your page | Verdict | Change |
| --- | --- | --- |
| 1. Microsoft Copilot Studio | Correct | Keep. It connects to a Fabric data agent as a connected agent or as a tool through Fabric IQ Data MCP. |
| 2. Azure AI Foundry Multi-agent ... | Duplicate | Merge into page 5. Azure AI Foundry is now Microsoft Foundry; multi-agent orchestration is one feature of the same Agent Service. |
| 3. Developer Tooling & ... | Correct if it covers the right tools | Make sure it covers GitHub Copilot, VS Code, Cursor and Claude Code connecting through MCP, plus the data agent Python SDK. |
| 4. Microsoft Agent Framework | Correct, with a note | Keep. It is Microsoft's open-source code framework, the successor to Semantic Kernel and AutoGen. It isn't a Fabric feature: it reaches Fabric through MCP or runs in a Fabric notebook. |
| 5. Microsoft Foundry Agent Service | Correct | Keep as the single Foundry page. Note the change: the "Microsoft Fabric" tool stops working on 6 November 2026; move to the Fabric IQ (OneLake catalog) tool. |
| (missing) Microsoft 365 Copilot and Teams | Add | A published data agent can be used directly in Microsoft 365 apps and Teams. |
| (missing) AI functions | Add | Row-by-row AI in notebooks, T-SQL and Dataflow Gen2. Not an agent, but a building block agents use. |

## Section 3: Fabric MCP

This section is empty; it should have one page per MCP server or endpoint. MCP (Model Context Protocol) is the open standard that lets any outside agent use Fabric with the signed-in user's own permissions.

| Page to add | What it is |
| --- | --- |
| 1. What is MCP in Fabric | One short overview: how an agent finds tools, signs in with Entra ID and calls Fabric. |
| 2. Fabric Core MCP Server | Remote endpoint; manages workspaces, items, folders and roles. Audit-logged. |
| 3. Fabric MCP Server (local) | Open source; runs on a developer machine, usually from the VS Code extension. API docs offline, OneLake files, Data Factory. |
| 4. Fabric IQ MCP | Remote and read-only; finds Power BI reports and semantic models and runs DAX. |
| 5. Data agent MCP endpoint | Every published data agent is also an MCP server with one "ask a question" tool. This replaced the retired Assistants API. |
| 6. Real-Time Intelligence and operations agent MCP | Lets agents query eventhouses, and create and run operations agents in plain language. |

## Corrected notebook structure

Copy this into OneNote: each top level is a section, and each item under it is a page.

1. **Native agents and assistants inside Fabric**
    1. Fabric data agent
    2. Operations agent
    3. Data engineering agent (Project Osmos)
    4. Ontology agent (Fabric IQ)
    5. Copilot for Data Factory
    6. Copilot for Power BI
    7. Copilot for Data Engineering and Data Science (notebooks)
    8. Copilot for Data Warehouse and SQL database
    9. Copilot for Real-Time Intelligence
2. **External agents and frameworks that use Fabric**
    1. Microsoft Copilot Studio
    2. Microsoft Foundry Agent Service (formerly Azure AI Foundry), including multi-agent orchestration
    3. Microsoft 365 Copilot and Teams
    4. Microsoft Agent Framework
    5. Developer tooling: MCP clients and the data agent Python SDK
    6. AI functions
3. **Fabric MCP**
    1. What is MCP in Fabric
    2. Fabric Core MCP Server
    3. Fabric MCP Server (local)
    4. Fabric IQ MCP
    5. Data agent MCP endpoint
    6. Real-Time Intelligence and operations agent MCP

## What each item is

One short explanation per page, ready to paste in as each page's opening. Status is as of October 2026.

### Section 1: Native agents and assistants

**Fabric data agent (GA).** A configurable virtual analyst that answers plain-English questions over up to five sources: lakehouses, warehouses, semantic models, KQL databases, ontologies and Microsoft Graph. It writes SQL, DAX or KQL behind the scenes, is strictly read-only, and runs with the asking user's permissions. Builders tune it with instructions and up to 100 example queries per source. Answers are capped at 25 rows and 25 columns.

**Operations agent (GA).** Watches live data in an eventhouse or ontology, checking each rule every 5 minutes. When a condition is met it recommends or takes an action: a Teams message, running a Fabric item, or a Power Automate flow. People approve each action unless a rule is marked autonomous. It has its own Entra Agent ID but acts with its creator's permissions.

**Data engineering agent, Project Osmos (preview).** An autonomous agent driven from a command line: GitHub Copilot CLI, Codex or Claude Code. You describe an end-to-end outcome and choose a lakehouse. It plans, writes and tests Spark notebooks, writes Delta tables and validates the results, and keeps running in Fabric after you disconnect. Its listed use cases include migration assistance.

**Ontology agent, part of Fabric IQ (preview).** Fabric IQ is the shared business context, made of OneLake, semantic models, ontology and graph, that gives agents a common business vocabulary. The ontology agent builds and maintains the ontology through conversation. It drafts definitions and data bindings and answers questions, and nothing changes until a person approves its proposal.

**Copilot for Data Factory (GA).** Writes Dataflow Gen2 queries, and generates, runs, summarises and troubleshoots pipelines.

**Copilot for Power BI (GA).** Writes DAX, suggests report pages and visuals, summarises data and answers questions. A full-screen standalone Copilot searches every report and model you can access; it is in preview and switched off by default.

**Copilot for Data Engineering and Data Science (preview).** A chat pane in notebooks that reads the notebook and its lakehouse schemas. It generates and refactors code across cells and applies changes only after you review a diff. "Fix with Copilot" explains failures and proposes fixes.

**Copilot for Data Warehouse and SQL database.** Generates, completes, fixes and explains T-SQL. It is in preview for the Warehouse and GA for SQL database, where it also works in SSMS and VS Code.

**Copilot for Real-Time Intelligence (GA).** Writes KQL queries and generates real-time dashboards.

The Copilots are preconfigured assistants inside each editor. Unlike agents, they can't be given custom instructions or published elsewhere.

### Section 2: External agents and frameworks

**Microsoft Copilot Studio.** A low-code platform for building agents. A Fabric data agent can be added as a connected agent, or as a tool through the Fabric IQ Data MCP option. Both must be in the same tenant, and the combination is validated for Teams.

**Microsoft Foundry Agent Service (formerly Azure AI Foundry).** Azure's platform for building, hosting and orchestrating agents, including multi-agent setups. A Foundry agent can call a Fabric data agent with the end user's own identity. The current "Microsoft Fabric" tool takes one data agent per Foundry agent and stops working on 6 November 2026. Agents must move to the Fabric IQ (OneLake catalog) tool before then.

**Microsoft 365 Copilot and Teams.** A published data agent can be used from Microsoft 365 apps and Teams. Purview policies still apply. Answers may leave Fabric's compliance boundary, so check data residency.

**Microsoft Agent Framework.** Microsoft's open-source code framework for building agents in Python and .NET, the successor to Semantic Kernel and AutoGen. Secondary sources report a 1.0 release at Build 2026. It isn't a Fabric feature: an agent built with it reaches Fabric through the MCP servers or a data agent's MCP endpoint, or runs inside a Fabric notebook.

**Developer tooling.** MCP clients such as GitHub Copilot, VS Code, Cursor, Claude Code and Claude Desktop connect to Fabric's MCP servers. The **data agent Python SDK** (`fabric-data-agent-sdk`, preview) creates, configures and publishes data agents in code. Published agents are then queried through their MCP endpoint.

**AI functions.** One-line AI transforms applied to every row, such as `ai.classify`, `ai.extract`, `ai.summarize` and `ai.translate`. They run in pandas, PySpark, T-SQL and Dataflow Gen2, using `gpt-5-mini` by default. They aren't agents, but agents and pipelines use them for bulk AI steps.

### Section 3: Fabric MCP

**What is MCP in Fabric.** MCP is an open standard that lets an AI agent find and call tools. In Fabric, every call uses the signed-in identity's own permissions, and connecting a server grants no extra access.

**Fabric Core MCP Server.** A remote endpoint with no install. It turns Fabric REST APIs into tools for managing workspaces, items, folders and role assignments. It signs in with Entra ID and its operations are audit-logged. Sources disagree on whether it is GA or preview.

**Fabric MCP Server (local).** An open-source server on GitHub that runs on a developer machine, usually through the VS Code extension. It provides API specs and best practices offline, OneLake file operations, item operations and Data Factory workflows. It doesn't write its own Fabric audit log.

**Fabric IQ MCP.** A remote, read-only server for Power BI. Agents use it to find reports and semantic models, read their metadata and run DAX, limited to what the user can see.

**Data agent MCP endpoint.** Each published data agent is an MCP server at `https://api.fabric.microsoft.com/v1/mcp/workspaces/{WorkspaceId}/dataagents/{DataAgentId}/agent`. It offers one tool that takes a question and returns an answer. It replaced the Azure OpenAI Assistants API route, retired on 26 August 2026.

**Real-Time Intelligence and operations agent MCP.** Lets outside agents query eventhouses, and create and run operations agents in plain language.

## Each agent in detail

This part covers the seven agents in the notebook: four built into Fabric and three built outside Fabric that use its data. The Copilots and MCP servers aren't agents, so they stay as summarised above. Each agent is described the same way: what it is, how it works, what it can reach, what it can and can't do, how to set it up, security, requirements and an example.

### 1. Fabric data agent

**What it is.** A Fabric item, generally available, that acts as a virtual analyst for one business domain. People ask questions in plain English and get answers from governed data in OneLake, without writing SQL, DAX or KQL. It is the conversational analytics piece in Microsoft's agent architecture: other agents call it when they need data.

**How it works, step by step**

1. **Checks the question** against responsible-AI rules, tenant policy and the user's permissions.
2. **Reads the schemas** the user is allowed to see, using the user's own credentials.
3. **Picks a data source.** It compares the question with each source, guided by the builder's instructions; for example, "finance questions go to the semantic model".
4. **Writes the query** with a matching tool: natural language to SQL for lakehouses and warehouses, to DAX for semantic models, to KQL for KQL databases, or a Microsoft Graph query.
5. **Validates and runs the query,** then returns a readable answer: a short table, a summary or key points.

**What it can reach**

- Up to five sources in any mix: lakehouses, warehouses, Power BI semantic models, KQL databases (including Eventhouse), mirrored databases, ontologies and Microsoft Graph.
- Only the tables the builder selects. Lakehouse files such as CSV or JSON must first be loaded into tables.
- For semantic models, Read permission is enough; Build permission isn't needed. Row-level and column-level security still apply.

**What it can and can't do**

- It can answer questions, explain its query, and keep context across follow-up questions.
- It can't write data, run notebooks or pipelines, or trigger any action. It is strictly read-only.
- It can't read PDFs, Word documents or other unstructured files.
- Answers are capped at 25 rows and 25 columns, so it is for insight, not exporting data.
- English only. You can't choose its language model.
- The agent and its data sources must be on capacities in the same region.

**How you set it up**

1. Create a Data agent item in a workspace and add the data sources.
2. Select the tables it may use.
3. Write **agent instructions**: which source answers which kind of question, and what business terms mean.
4. Add **example queries**, up to 100 question-and-query pairs per source; semantic models don't support them. The preview **Build agent with AI** mode can draft instructions and examples for SQL and Eventhouse sources.
5. Test it, then **publish** it. Publishing also turns on its MCP endpoint.
6. Manage versions with Git integration and deployment pipelines, and use built-in diagnostics to see how it chose sources.

**Where people use it:** in Fabric, Microsoft 365 Copilot, Teams, Copilot Studio, Microsoft Foundry, Copilot in Power BI, and any MCP client through `https://api.fabric.microsoft.com/v1/mcp/workspaces/{WorkspaceId}/dataagents/{DataAgentId}/agent`.

**Security.** Every query runs as the person asking. Rules apply in a fixed order: tenant policy first, then workspace roles, then the builder's instructions, then the user's question. A prompt can't override a policy. Purview DLP and access-restriction policies can trim or block answers, and conversations can be audited and found in eDiscovery. Chat history is kept for up to 28 days unless cleared.

**Requirements:** a paid F2 or higher capacity, or Power BI Premium P1 or higher with Fabric enabled. The Copilot and Azure OpenAI tenant setting must be on, and the cross-geo AI settings too, if the capacity is outside the US or EU.

**Example.** A finance data agent over the sales semantic model and the orders warehouse. A manager asks in Teams, "Which region missed its Q3 target, and by how much?" The agent writes DAX against the model, applies the manager's row-level security, and returns a three-row table.

### 2. Operations agent

**What it is.** A Real-Time Intelligence item, generally available, that watches live data for one business process and responds when something needs attention. Unlike the data agent, it is proactive: it doesn't wait for a question, and it can take action.

**How it works, step by step**

1. **You describe the goal** in plain language, for example "Monitor the turbines and alert me when motor temperature gets too high". A built-in Copilot chat helps turn it into instructions and rules, and warns you when an instruction is unclear or refers to data that doesn't exist.
2. **It builds a playbook** when you select Generate Playbook. The playbook lists the goals, the business properties it watches (mapped to columns), the rules, and the query behind each rule. You can copy any rule's query into a KQL queryset to test it.
3. **It checks every rule every 5 minutes** against the data source. A *state* condition, such as "is above", fires on every check while it is true. A *transition* condition, such as "crosses above", fires once when the value changes into the condition. The conditions are is above, crosses above, is below, crosses below, enters range, exits range, is, and becomes.
4. **When a rule fires, it reasons about the situation** and sends a Teams message with a summary and a recommended action.
5. **For anomalies, it investigates** with Investigator insights, in preview. *Investigate further* in Teams shows the scope, key observations (actual values, deviations, trends) and the patterns that changed around the anomaly.
6. **A person approves or rejects** the recommendation, unless the rule is marked autonomous, in which case it acts straight away.
7. **Everything is recorded** in the activity log: every condition checked, recommendation made and response received.

**What it can reach:** an eventhouse KQL database, or a Fabric IQ ontology, in the same workspace.

**Actions it can take**

- **Teams message.** This is the default and needs no setup beyond installing the *Fabric Operations Agent* Teams app. Messages go to an individual or a Teams channel. Recipients must be in the organisation and have write permission on the agent item.
- **Fabric item.** Run a notebook, pipeline, user-defined function or other Fabric item. Each action has a name and a description, which the agent uses to choose it, plus optional parameters the agent fills in.
- **Power Automate flow.** Linked through an Activator item that stores the connection. The flow receives the agent's parameters as dynamic content.

**What it can't do:** it doesn't run on trial capacities, and it watches only its configured eventhouse or ontology. It runs on a 5-minute cycle, so it isn't suited to sub-minute alerting.

**Security.** Each agent gets its own **Microsoft Entra Agent ID**, a dedicated identity that shows in the Entra admin center, so its actions are logged separately from people's. It runs in *delegated* mode: it acts with its creator's permissions through an on-behalf-of flow. When anyone approves a recommendation, the action runs with the creator's permissions, not the approver's. Review autonomous rules carefully, and review the agent when its creator leaves or changes role.

**Requirements.** A paid Fabric capacity, an eventhouse with a KQL database or an ontology, and a Teams account. Admin settings for operations agents, Copilot and Azure OpenAI must be on, plus the cross-geo AI settings if the capacity is outside the US or EU.

**Cost** has three parts: background monitoring compute, AI reasoning each time a condition fires, and OneLake storage. Copilot use and eventhouse queries are charged on top.

**Example.** An agent watches a pipeline-runs table in an eventhouse. When a nightly load fails twice in a row, it posts in the data team's Teams channel with the failed pipeline and the error. It recommends rerunning the pipeline, which is a Fabric item action, and the on-call engineer approves it from Teams.

### 3. Data engineering agent (Project Osmos)

**What it is.** An autonomous data engineering agent, in preview, code-named Project Osmos. You give it a whole outcome rather than a question, and it plans, builds, runs and checks the work in Fabric. Of the native agents it is the only one that writes code and data on its own. Microsoft describes each task as "a managed unit of data engineering work rather than a transient chat response".

**How it works, step by step**

1. **Define.** From a supported command-line client (GitHub Copilot CLI, Codex or Claude Code), you write the *project outcome*: the complete end-to-end task. You also give the URL of the lakehouse that hosts it, which becomes the Spark session's default lakehouse.
2. **Plan.** It studies the context and proposes operating settings for you to review: permissions, write safety, rerun behaviour, schema evolution, which artifacts to keep, and how much reasoning effort to spend.
3. **Execute.** It runs in Fabric Spark and OneLake, not on your laptop. It inspects data, writes and tests Spark notebooks, transforms data and writes Delta tables. Several coordinated agents build and compare different implementations to reach production quality.
4. **Validate.** It runs the generated code and checks the results against your success criteria: row counts, nulls, schemas and any rule you gave.
5. **Monitor.** The task keeps running after the client disconnects and can be resumed without starting over. Progress, activity, errors and outputs show both in the CLI and in the lakehouse's task view, under the same task ID.

**What it can reach:** anything your Fabric permissions allow. The chosen lakehouse is only the default; the outcome, your permissions and the reviewed settings decide the real scope.

**Good fits, from Microsoft's list**

- **Data preparation:** clean files, standardise types, remove bad rows, write a validated Delta table.
- **Table integration:** join orders, customers and products into a curated table.
- **Lakehouse modernisation:** convert an existing transformation to Spark, with a tested notebook.
- **Medallion architecture:** build bronze, silver and gold layers as one task.
- **Data quality:** profile a table, apply approved fixes, report before-and-after metrics.
- **Performance:** review Delta table layout, apply approved partitioning changes, measure the result.
- **Migration assistance:** turn legacy extracts into a Fabric-ready model while keeping business rules and checking totals.

**What it can't do, or isn't for:** quick queries, Power BI report changes or anything outside data engineering. As a preview it isn't meant for production, has no SLA, and may be limited to some regions.

**Security.** It works with your Fabric permissions. Write safety and permissions are among the settings you approve before it runs. Anyone with access to the lakehouse can see the task and give it input, so treat the lakehouse's access list as the task's audience.

**Requirements.** A Fabric lakehouse on a capacity, and one of the supported CLI clients signed in to Fabric.

**Example.** "Take the raw CSV order extracts in Files/landing, build bronze, silver and gold Delta tables, deduplicate on order ID, and make sure gold totals match the source within 0.1%." It proposes settings and you approve them. It builds and tests the notebooks, writes the tables, and reports the totals check. Its migration-assistance use case is the closest overlap with our accelerator, which already calls Claude Code.

### 4. Ontology agent (Fabric IQ)

**What it is.** A built-in agent, in preview, that helps people build and use a Fabric IQ **ontology** through conversation. To understand the agent you need the ontology first.

**The ontology it works on.** An [ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) is a Fabric item, in preview, that describes the business in business terms, separate from how tables are stored. It is made of:

- **Entity types:** business concepts such as Customer, Shipment or Machine, with their **properties**.
- **Relationships:** for example *Customer places Order* or *Machine belongs to Production Line*.
- **Metrics:** trusted calculations carried over from Power BI DAX measures. The semantic model stays the owner of the formula.
- **Rules:** business logic written in plain language and linked to entities.
- **Namespaces, inheritance and shared properties,** to organise and reuse definitions.
- **Data bindings:** links from each definition to live data in semantic models, lakehouses, eventhouses, warehouses, SQL databases, mirrored databases and OneLake shortcuts or views. Data stays where it is; the ontology doesn't copy it.

**How the agent works**

1. **Generates definitions.** It reads connected Fabric sources and proposes entity types, properties, relationships and data bindings.
2. **Answers business questions** against the ontology, using its entities, rules and metrics rather than raw table names.
3. **Proposes changes; it doesn't apply them.** Everything follows a *proposal-first* model: a person reviews each proposed change before it is applied.

**Other ways to build an ontology,** which the agent can then extend:

- Generate it from an existing Power BI semantic model. This carries over tables, relationships, calculated columns and DAX measures, the last as metrics.
- Build it from scratch in the editor.
- Import RDF, Turtle or OWL definitions into an empty ontology. The import reports what was kept, adjusted or unsupported. It can also be exported as RDF or Turtle.

**What it enables for other agents:** a data agent can use an ontology as a source, and an operations agent can monitor one. They then reason in business terms, such as "shipments at risk", instead of joins. An optional **graph** can be built from the ontology for multi-hop questions such as dependency chains or shortest paths. Graph data isn't loaded by default; you choose to materialise it.

**What it can't do:** the ontology isn't a query engine. Queries always run against the bound source systems. The agent's changes never apply without approval.

**Security.** Workspace and item permissions control who can see or edit the ontology. Queries respect OneLake security and the source's row-level, object-level and column-level security.

**Lifecycle.** Named versions can be saved and restored, and the ontology supports CI/CD and APIs. The *old* ontology experience retires on 31 January 2027. Existing ontologies are copied to the new experience with an in-product button; rules and Activator alerts must be recreated by hand.

**Example.** Generate an ontology from the sales semantic model, so revenue and margin measures become metrics. Then ask the ontology agent to add a *Shipment* entity bound to the logistics eventhouse, related to *Order*. It proposes the definition and binding, you approve, and an operations agent can now watch "late shipments for high-value customers".

### 5. Copilot Studio agents that use Fabric

**What it is.** [Microsoft Copilot Studio](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-microsoft-copilot-studio) is Microsoft's graphical, low-code platform for building custom agents. They understand natural language, answer questions and automate tasks, and are published to channels such as Teams, websites and Microsoft 365 Copilot. A Copilot Studio agent uses Fabric by calling a published Fabric data agent. This agent-to-agent pattern is in preview.

**How it works.** The Copilot Studio agent is the front door and orchestrator. It handles the conversation, its own topics, knowledge sources (SharePoint, websites, files) and tools. When a question needs enterprise data, its generative orchestration hands that part to the Fabric data agent. The data agent answers from OneLake, and the Copilot Studio agent folds that answer into its reply.

**Two ways to connect**

- **As a connected agent:** Agents → Add → Microsoft Fabric, then pick the data agent from the list you can access.
- **As a tool:** Add a tool → search for Fabric → *Fabric IQ Data MCP*. This newer route calls the data agent's MCP endpoint.

**How you set it up**

1. Make sure the Fabric data agent works and is **published**, with a rich description; Copilot Studio uses it to decide when to call the agent.
2. Sign in to Fabric and Copilot Studio with the **same account, in the same tenant**.
3. Create or open an agent in Copilot Studio and give it a name and description.
4. Add the Fabric data agent, as a connected agent or as a tool.
5. Choose authentication. **User authentication** runs as each end user, who then needs access to the data agent and its sources. **Agent author authentication** runs as the builder.
6. Turn on **generative AI orchestration** under Settings → Orchestration.
7. Test in the chat pane, then publish and choose the channels.

**What it can and can't do**

- It can combine Fabric data with other knowledge, tools, actions and other agents in one conversation.
- Microsoft has validated the connected Fabric data agent only for **Teams**. Microsoft 365 Copilot isn't currently supported for this combination; other channels may work but are untested.
- It can only read Fabric data, because the data agent behind it is read-only.

**Security.** With user authentication, every user needs at least read access to the data agent and its underlying sources. Answers returned to Copilot Studio may leave Fabric's compliance boundary and region and fall under Copilot Studio's terms.

**Requirements.** A paid F2 or higher capacity (or P1 or higher), the AI cross-geo tenant settings where required, a Microsoft 365 Copilot license, and a Copilot Studio user license for each person who builds agents.

**Example.** An HR assistant in Teams answers policy questions from SharePoint. When someone asks "how many open positions do we have in Hyderabad?", it calls the workforce data agent, which queries the HR warehouse.

### 6. Microsoft Foundry agents that use Fabric

**What it is.** Microsoft Foundry, formerly Azure AI Foundry, is Azure's platform for building, testing and hosting agents with the model of your choice. Its Agent Service runs agents that combine instructions, tools, knowledge and other agents, including multi-agent setups. A Foundry agent uses Fabric by calling one or more published Fabric data agents as tools.

**How it works**

1. A user asks the Foundry agent a question.
2. The Foundry agent decides, guided by your instructions, whether a Fabric data agent can answer it, and which one.
3. It calls that data agent through its MCP endpoint.
4. The data agent queries OneLake *as the signed-in user*, through delegated (on-behalf-of) authentication, so Fabric applies that user's permissions.
5. The Foundry agent combines the answer with its own reasoning and other tools, and replies.

The model you pick in Foundry only orchestrates and writes replies. It doesn't change the model inside the data agent.

**Two tools, and a deadline**

| | Old "Microsoft Fabric" tool | New "Fabric IQ (OneLake Catalog)" tool |
| --- | --- | --- |
| Data agents per Foundry agent | One | Several, one tool entry each |
| How you choose one | Type the workspace ID and artifact ID | Pick from the OneLake catalog, filtered to data agents you can access |
| Connection | Fabric connection with ID keys | MCP endpoint per data agent |
| Status | **Stops working on 6 November 2026** | Preview, the replacement |

**How you set it up, with the new tool** ([guide](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-foundry-fabric-iq))

1. Publish the data agent, and check that it answers correctly in Fabric.
2. In the Foundry portal, open the project, then Build, New agent, Build an agent.
3. Under Tools, choose Add, Browse all tools, **Fabric IQ (OneLake Catalog)**.
4. Filter the catalog to *Data agent* and add one. Repeat for each business area, such as sales, supply chain or support.
5. Write **instructions** that name each data agent and what it handles, for example "Use Contoso Sales for revenue and orders; use Contoso Support for tickets; if a question needs both, call both."
6. **Save**; nothing is kept until you do. Then test every data agent in the Playground.
7. For slow questions, choose a model that supports **background mode** and turn it on.

In code (Python, C#, JavaScript or REST; Java isn't supported), pass the data agent's MCP endpoint as `server_url` and the Fabric IQ project connection as `project_connection_id`.

**Roles.** Foundry User builds and tests agents. Foundry Project Manager creates the Fabric IQ connection. Foundry Owner or Azure Owner/Contributor creates a project.

**What it can and can't do:** a Foundry agent can mix Fabric data with other tools, knowledge and agents, and run multi-agent flows. Through Fabric it can only read, because data agents are read-only. Both sides must be in the same tenant, and Fabric IQ isn't available in regions where Power BI is the only Fabric workload.

**Security.** Every call runs as the signed-in user. Answers returned to Foundry may leave Fabric's compliance boundary and region. Foundry shows observability for each call: which sources the data agent queried and how long each step took.

**Example.** A supply-chain Foundry agent with three data agents: inventory, orders and supplier performance. Asked "which late suppliers affect our top 10 products?", it calls the orders and supplier agents and joins the answers.

### 7. Microsoft Agent Framework agents that use Fabric

**What it is.** [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/agent-framework-overview) is Microsoft's open-source code framework for building agents and multi-agent workflows in Python and .NET; Go is in preview. It is the direct successor to **Semantic Kernel** and **AutoGen**, built by the same teams. It combines AutoGen's simple agent patterns with Semantic Kernel's enterprise features: sessions, type safety, middleware and telemetry. Of everything in the notebook, it is the only true agent *framework*. It isn't a Fabric feature; you write code with it, and that code uses Fabric.

**Its four main parts**

- **Agents:** an LLM plus instructions, tools and MCP servers. Models can come from Microsoft Foundry, Azure OpenAI, OpenAI, Anthropic, Ollama and others.
- **Harness agent:** a ready-made agent for long multi-step tasks, with planning and to-do tracking, context compaction, file access and memory, tool approval and observability.
- **Workflows:** graph-based or functional flows that connect agents and functions in a fixed order. They support long-running and human-in-the-loop scenarios.
- **Integrations:** model providers, agent services, tools, memory (context providers), middleware, evaluation and UI frameworks.

Microsoft's own rule of thumb: use an agent for open-ended tasks, and a workflow when the steps are well defined. If a plain function can do the job, don't use an agent.

**How it uses Fabric.** There's no special Fabric connector; it reaches Fabric through MCP:

1. **A Fabric data agent as a tool:** point the framework's MCP client at the data agent's endpoint, `https://api.fabric.microsoft.com/v1/mcp/workspaces/{WorkspaceId}/dataagents/{DataAgentId}/agent`, with an Entra token.
2. **Fabric MCP servers as tools:** the Core MCP Server lets the agent manage workspaces and items; Fabric IQ MCP lets it run DAX on Power BI models.
3. **Inside Fabric:** install the package with `pip install agent-framework` in a Fabric notebook, so the agent works next to Spark and OneLake data and can call AI functions.
4. **Through Foundry:** run the agent on Foundry models, or as a Foundry-hosted agent, and use the Fabric IQ tool there.

**What it can and can't do:** anything you code. It can read, write and orchestrate many agents, which is both its power and its risk. Nothing is governed for you: you own the model choice, secrets, content safety, responsible-AI controls, testing and cost. Microsoft's own notice says so, and warns that data sent to third-party models or servers may leave your compliance boundary.

**Security.** Calls into Fabric use whatever identity your code authenticates with, a user or a service principal. Fabric's permissions still apply on its side.

**Status.** The Microsoft Learn overview (updated July 2026) shows Python and .NET as the main languages and Go in preview. Secondary sources report a 1.0 general-availability release at Build 2026.

**Example.** A Python workflow in a Fabric notebook. One agent reads a failed pipeline's error through the Core MCP Server. A second asks the data agent which downstream reports use that table. A human-approval step then decides whether to rerun the pipeline.

### The seven agents side by side

| Agent | Kind | Who builds it | Starts work when | Can change data or items | Status (Oct 2026) |
| --- | --- | --- | --- | --- | --- |
| Fabric data agent | Built-in, no code | Analysts | Someone asks a question | No, read-only | GA |
| Operations agent | Built-in, no code | Operations teams | A rule fires on live data, every 5 minutes | Yes, through approved or autonomous actions | GA |
| Data engineering agent (Osmos) | Built-in, autonomous | Data engineers, from a CLI | You give it an outcome | Yes, notebooks and Delta tables | Preview |
| Ontology agent | Built-in assistant | Data and domain modellers | You ask it to model or answer | Proposes changes; a person approves | Preview |
| Copilot Studio agent | External, low-code | Makers | Someone chats in Teams or another channel | Only through its own actions; Fabric side is read-only | Preview (Fabric link) |
| Foundry agent | External, portal or code | AI developers | Someone chats or an app calls it | Only through its own tools; Fabric side is read-only | Preview (Fabric IQ tool) |
| Agent Framework agent | External, code | Developers | Whatever your code decides | Anything you code | Open source; GA reported |

The data agent is the common building block: Copilot Studio and Foundry reach Fabric data through it, and Agent Framework usually does too.

## Sources

Microsoft Learn pages, opened 14 to 16 October 2026:

- [Fabric data agent concepts](https://learn.microsoft.com/en-us/fabric/data-science/concept-data-agent)
- [Create and configure operations agents](https://learn.microsoft.com/en-us/fabric/real-time-intelligence/operations-agent)
- [Data engineering agent (Project Osmos)](https://learn.microsoft.com/en-us/fabric/data-engineering/data-engineering-agent-overview)
- [What is Fabric IQ?](https://learn.microsoft.com/en-us/fabric/iq/overview)
- [Release status of AI and Copilot in Fabric](https://learn.microsoft.com/en-us/fabric/fundamentals/copilot-ai-feature-state)
- [Consume a data agent in Microsoft Foundry](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-foundry)
- [Consume a data agent in Copilot Studio](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-microsoft-copilot-studio)
- [Fabric data agent Python SDK](https://learn.microsoft.com/en-us/fabric/data-science/fabric-data-agent-sdk)
- [AI functions overview](https://learn.microsoft.com/en-us/fabric/data-science/ai-functions/overview)
- [Fabric MCP servers overview](https://learn.microsoft.com/en-us/rest/api/fabric/articles/mcp-servers/what-is-fabric-mcp-server)
- [MCP in Real-Time Intelligence](https://learn.microsoft.com/en-us/fabric/real-time-intelligence/mcp-overview)

The Microsoft Agent Framework status comes from secondary sources found on 26 October 2026, such as [this Build 2026 summary](https://www.arcade.dev/blog/microsoft-build-2026-agent-stack). Confirm it on Microsoft Learn before quoting it.

Added for "Each agent in detail", opened 26 October 2026:

- [What is ontology (preview)?](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview)
- [Operations agent actions](https://learn.microsoft.com/en-us/fabric/real-time-intelligence/operations-agent-actions)
- [Consume a data agent from Foundry via Fabric IQ](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-foundry-fabric-iq)
- [Microsoft Agent Framework overview](https://learn.microsoft.com/en-us/agent-framework/overview/agent-framework-overview)
