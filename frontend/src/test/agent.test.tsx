import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { resolve, settingsFrom } from "../agent/brief";
import { readFacts } from "../agent/facts";
import { mockApi, resetDemo } from "../mock/mockApi";

const CONSULT = "ma.consult.mock.default";
const JOURNEY = "ma.journey.mock.default";
const stored = (key: string) => JSON.parse(localStorage.getItem(key) ?? "null");

function go(path: string) {
  window.history.pushState({}, "", path);
  return render(<App />);
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ma.apiMode", "mock");
  window.history.pushState({}, "", "/");
  resetDemo();
  // Discover (with its assessment and waves) is confirmed: the consultation and the orchestrator are open.
  localStorage.setItem(JOURNEY, JSON.stringify(["discover", "assess", "waves"]));
});

describe("consultation and agent orchestrator", () => {
  it("comes after Discover as step 2, and hands over to the agent orchestrator as step 3", async () => {
    const user = userEvent.setup();
    go("/migration");
    expect(await screen.findByRole("heading", { level: 2, name: "Consultation" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByText("The brief is not approved yet")).toBeInTheDocument();
    // Without a brief the plan, run and validation can still be worked by hand.
    await user.click(screen.getByRole("button", { name: "Continue without an approved brief" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Agent orchestrator" })).toBeInTheDocument();
    for (const t of ["Agent run", "Plan", "Migration", "Validation"]) expect(screen.getByRole("tab", { name: new RegExp(t) })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Plan/ }));
    expect(await screen.findByRole("heading", { name: "Build the migration plan" })).toBeInTheDocument();
  }, 20000);

  it("recommends from discovery, needs a person for the sign-off, and approves a version of the brief", async () => {
    const user = userEvent.setup();
    go("/migration?step=consult");
    const question = await screen.findByRole("heading", { level: 3, name: "What is the migration scope?" }, { timeout: 8000 });
    const card = question.closest("section")!;
    expect(within(card).getByText(/^Recommended:/)).toBeInTheDocument();
    // Every question says what its answer does.
    expect(within(card).getByText("Drives the run")).toBeInTheDocument();

    // Typed text is matched to the question's options without an AI model.
    await user.type(screen.getByRole("textbox", { name: "Ask or describe" }), "?{Enter}");
    expect(await within(screen.getByRole("list", { name: "Consultation notes" })).findByText(/Suggested:/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Review & approve/ }));
    // The sign-off has no recommendation: it holds the brief back until a person answers it.
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Answer it" }));
    await user.type(await screen.findByRole("textbox", { name: "Who approves production cutover for the client?" }), "Priya Shah, data lead");
    await user.click(screen.getByRole("button", { name: /Save answer/ }));

    await user.click(screen.getByRole("button", { name: /Review & approve/ }));
    await user.click(screen.getByRole("button", { name: "Accept all recommendations" }));
    // Submitting waits until the features discovery found are read, so the questions cannot change under it.
    await waitFor(() => expect(screen.getByRole("button", { name: "Submit for approval" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Submit for approval" }));
    expect(await screen.findByText(/Version 1 is submitted and waits for approval/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Approved by" })).toHaveValue("Priya Shah, data lead");
    await user.click(screen.getByRole("button", { name: /Approve version 1/ }));
    expect(await screen.findByText(/Version 1 is approved by Priya Shah, data lead/)).toBeInTheDocument();

    // Changing an answer afterwards means a new version must be approved.
    await user.click(screen.getByRole("button", { name: /Scope and approach/ }));
    await user.click(screen.getAllByRole("button", { name: "Change" })[0]);
    const scope = await screen.findByRole("radiogroup", { name: "What is the migration scope?" });
    await user.click(within(scope).getByRole("radio", { name: /Migrate all discovered objects/ }));
    await user.click(screen.getByRole("button", { name: /Save answer/ }));
    await user.click(screen.getByRole("button", { name: /Review & approve/ }));
    expect(await screen.findByText(/The answers changed since the last approved version/)).toBeInTheDocument();
    expect(stored(CONSULT).versions).toHaveLength(1);
  }, 40000);

  it("asks about a feature only when discovery found it, and reads what the configuration already says", async () => {
    const user = userEvent.setup();
    go("/migration?step=consult");
    await screen.findByRole("heading", { level: 3, name: "What is the migration scope?" }, { timeout: 8000 });
    await waitFor(() => expect(screen.queryByText(/Reading what discovery found/)).toBeNull());
    const topics = screen.getByRole("navigation", { name: "Topics" });
    // The demo estate has Spark pools, notebooks using mssparkutils and pipelines: their questions are asked.
    for (const t of ["SQL pool and SQL objects", "Spark pools and notebooks", "Pipelines, triggers and connections"]) {
      expect(within(topics).getByRole("button", { name: new RegExp(t) })).toBeInTheDocument();
    }
    await user.click(within(topics).getByRole("button", { name: /Spark pools and notebooks/ }));
    expect(await within(topics).findByRole("button", { name: /mssparkutils/ })).toBeInTheDocument();
    // The Fabric workspace is connected: it is read, not asked.
    await user.click(within(topics).getByRole("button", { name: /Target architecture/ }));
    await user.click(within(topics).getByRole("button", { name: /Which Fabric workspace/ }));
    const workspaceCard = (await screen.findByRole("heading", { level: 3, name: /Which Fabric workspace/ })).closest("section")!;
    expect(within(workspaceCard).getByText("Fabric_demo")).toBeInTheDocument();
    expect(within(workspaceCard).getByText("From configuration")).toBeInTheDocument();
    expect(within(workspaceCard).getByText(/Not asked: change it on the Connections page/)).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: /Which Fabric workspace/ })).toBeNull();
  }, 20000);

  it("leaves out questions about features the estate does not have", async () => {
    const graph = await mockApi.getDependencies();
    const capabilities = await mockApi.getCapabilities();
    const base = { discovery: await mockApi.getDiscoveryStatus(), connection: await mockApi.getConnection(), graph, capabilities, fabricConnected: true, fabricGit: null };
    const asked = (consultFacts: Parameters<typeof readFacts>[0]["consultFacts"]) =>
      resolve(readFacts({ ...base, consultFacts }), {}).filter((r) => r.applies).map((r) => r.q.id);
    const none = asked({ facts: { mssparkutilsNotebooks: 0, dynamicSqlObjects: 0, selfHostedRuntimes: 0, linkedServicesOnSelfHostedRuntime: 0 }, unreadable: [], notRecorded: [] });
    expect(none).not.toContain("spark.utils");
    expect(none).not.toContain("sql.dynamic");
    expect(none).not.toContain("pipelines.runtime");
    const found = asked({ facts: { mssparkutilsNotebooks: 3, dynamicSqlObjects: 2, selfHostedRuntimes: 1, linkedServicesOnSelfHostedRuntime: 2 }, unreadable: [], notRecorded: [] });
    expect(found).toEqual(expect.arrayContaining(["spark.utils", "sql.dynamic", "pipelines.runtime"]));
  });

  it("turns the approved answers into the run's stages, options and prerequisites", async () => {
    const graph = await mockApi.getDependencies();
    const capabilities = await mockApi.getCapabilities();
    const facts = readFacts({ discovery: await mockApi.getDiscoveryStatus(), connection: await mockApi.getConnection(), graph, capabilities, fabricConnected: true, fabricGit: null,
      consultFacts: { facts: { selfHostedRuntimes: 1, linkedServicesOnSelfHostedRuntime: 1 }, unreadable: [], notRecorded: [] } });
    const s = settingsFrom(facts, resolve(facts, {
      "scope.mode": "selected", "scope.stages": ["notebooks", "warehouse"], "data.load": "none", "validation.failures": "stop",
      "target.collation": "case_sensitive", "pipelines.runtime": "gateway", "security.network": "no", "validation.gate": "all",
    }));
    expect(s.stages).toEqual(["warehouse", "notebooks"]);
    expect(s.options).toMatchObject({ stopOnFailure: true, collation: "case_sensitive", stages: ["warehouse", "notebooks"] });
    expect(s.planTypes).toContain("Notebook");
    expect(s.planTypes).not.toContain("Pipeline");
    expect(s.prerequisites).toEqual([expect.stringMatching(/on-premises data gateway/)]);
    expect(s.validationGate).toBe("all");
    // A pilot moves the first wave only; a Lakehouse keeps the SQL stages and sets the run's target, which
    // makes Delta tables and turns stored procedures into notebooks.
    expect(settingsFrom(facts, resolve(facts, { "scope.mode": "pilot" })).waves).toEqual([1]);
    expect(s.options.target).toBe("warehouse");
    const lake = settingsFrom(facts, resolve(facts, { "target.sql": "lakehouse" }));
    expect(lake.stages).toContain("warehouse");
    expect(lake.options.target).toBe("lakehouse");
  });

  it("drives validation's value profile from the reconciliation answer, and lists what a person signs off before cutover", async () => {
    const facts = readFacts({ discovery: await mockApi.getDiscoveryStatus(), connection: await mockApi.getConnection(), graph: await mockApi.getDependencies(),
      capabilities: await mockApi.getCapabilities(), fabricConnected: true, fabricGit: null,
      consultFacts: { facts: { dynamicSqlObjects: 4 }, unreadable: [], notRecorded: [] } });
    const exact = settingsFrom(facts, resolve(facts, {}));
    expect(exact.validation.reconcile).toMatchObject({ profile: true, tolerancePct: 0, differences: "mismatch" });
    expect(settingsFrom(facts, resolve(facts, { "validation.tolerance": "0.1" })).validation.reconcile).toMatchObject({ profile: true, tolerancePct: 0.1 });
    expect(settingsFrom(facts, resolve(facts, { "validation.tolerance": "business" })).validation.reconcile).toMatchObject({ differences: "review" });
    expect(settingsFrom(facts, resolve(facts, { "validation.tolerance": "counts" })).validation.reconcile?.profile).toBe(false);
    const s = settingsFrom(facts, resolve(facts, {
      "sql.sla": "benchmark", "scope.rollback": "formal", "scope.downtime": "4h", "validation.parallel": "no", "sql.dynamic": "review",
    }));
    expect(s.signoffs).toEqual(expect.arrayContaining([
      expect.stringMatching(/benchmarked performance-critical queries/), expect.stringMatching(/Formal rollback/),
      expect.stringMatching(/downtime: 1–4 hours/), expect.stringMatching(/reviewed each of the 4 objects using dynamic SQL/),
    ]));
    expect(s.signoffs.some((x) => /in parallel/.test(x))).toBe(false);
    // The SLA answer turns on Validate's benchmark; thresholds make a slowdown a mismatch.
    expect(s.validation.performance).toEqual({ enabled: true, thresholdPct: 50, limit: 10, strict: false });
    expect(settingsFrom(facts, resolve(facts, { "sql.sla": "thresholds" })).validation.performance).toMatchObject({ enabled: true, thresholdPct: 20, strict: true });
    expect(settingsFrom(facts, resolve(facts, { "sql.sla": "none" })).validation.performance?.enabled).toBe(false);
    expect(settingsFrom(facts, resolve(facts, { "validation.results": "both" })).validation.reconcile).toMatchObject({ views: true, procedures: true });
    expect(s.validation.access).toBe(true);
  });

  it("walks the demo cutover: behind, final sync, schedules on, rollback, approval", async () => {
    const before = await mockApi.getCutover();
    expect(before.freshness.behind).toBe(1);
    expect(before.checks.find((c) => c.label === "Synapse triggers stopped")?.status).toBe("person");
    await mockApi.runFinalSync();
    expect((await mockApi.getCutover()).freshness.behind).toBe(0);
    expect((await mockApi.setSchedules(true)).changed).toHaveLength(3);
    expect((await mockApi.getCutover()).schedules.every((x) => x.enabled)).toBe(true);
    expect((await mockApi.setSchedules(false)).changed).toHaveLength(3);
    await mockApi.completeCutover("Test lead");
    expect((await mockApi.getCutover()).record?.approvedBy).toBe("Test lead");
    const capacity = await mockApi.getCapacity();
    expect(capacity.recommendation.sku).toBe("F64");
    expect(capacity.caveats.some((c) => /assumed/.test(c))).toBe(true);
  });

  it("runs the approved brief end to end: plan, approval, run, validation, sign-off", async () => {
    const user = userEvent.setup();
    // A small scope keeps the demo run short: notebooks only, no data, no connections.
    localStorage.setItem(CONSULT, JSON.stringify({
      answers: {
        "scope.mode": "selected", "scope.stages": ["notebooks"], "data.load": "none", "security.credentials": "skip", "pipelines.triggers": "skip",
        "security.roles": "no", "security.network": "unknown", "validation.gate": "critical", "context.owner": "Test lead",
      },
      notes: [], versions: [],
    }));
    go("/migration?step=consult");
    await user.click(await screen.findByRole("button", { name: /Review & approve/ }, { timeout: 8000 }));
    const submit = await screen.findByRole("button", { name: "Submit for approval" });
    await waitFor(() => expect(submit).toBeEnabled(), { timeout: 5000 });
    await user.click(submit);
    await user.click(await screen.findByRole("button", { name: /Approve version 1/ }));
    await user.click(await screen.findByRole("button", { name: "Go to Agent orchestrator" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Agent orchestrator" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Start the agent/ }));
    // The network is unknown, so a connectivity check is a prerequisite a person confirms; the agent never does it.
    const prereq = await screen.findByRole("group", { name: "Confirm the prerequisites" }, { timeout: 15000 });
    expect(within(prereq).getByText(/Fabric can reach the source systems/)).toBeInTheDocument();
    await user.click(within(prereq).getByRole("button", { name: "They are in place, continue" }));
    // The brief asks for approval before writing: the agent stops there.
    const approve = await screen.findByRole("button", { name: "Approve and start the run" }, { timeout: 15000 });
    expect(screen.getByRole("group", { name: /Approve writing to/ })).toBeInTheDocument();
    await user.click(approve);
    const stages = screen.getByRole("navigation", { name: "Agent stages" });
    // No hand-over: after validation, the agent waits for a person to sign off what the brief names before cutover.
    expect(within(stages).queryByRole("button", { name: /Hand-over/ })).toBeNull();
    await waitFor(() => expect(within(stages).getByRole("button", { name: /Validation/ })).toHaveTextContent(/checks/), { timeout: 60000 });
    const signoff = await screen.findByRole("group", { name: "Sign off before cutover" }, { timeout: 15000 });
    expect(within(signoff).getByText(/Synapse is still in service as the fallback/)).toBeInTheDocument();
    await user.click(within(signoff).getByRole("button", { name: "Every item is done: sign off" }));
    await waitFor(() => expect(within(stages).getByRole("button", { name: /Cutover sign-off/ })).toHaveTextContent(/signed off by a person/));
    expect(within(stages).getByRole("button", { name: /Migration run/ })).toHaveTextContent(/created/);
    // The application's activity log records what the agent did, and what the run did.
    const activity = (stored("ma.activity.mock.default") as { source: string; text: string }[]);
    // A page loading is not an action: connections already there when the page opened are not recorded.
    expect(activity.some((e) => /Source connected/.test(e.text))).toBe(false);
    expect(activity.some((e) => /^Migration run .* started/.test(e.text))).toBe(true);
    expect(activity.some((e) => e.source === "agent")).toBe(true);
    expect(activity.some((e) => /Brief v1 approved/.test(e.text))).toBe(true);
    // The steps follow the agent: Plan, Migration and Validation are confirmed as it goes.
    expect(stored(JOURNEY)).toEqual(["discover", "assess", "waves", "plan", "migrate", "validate"]);
    expect(screen.getByRole("tab", { name: /Validation/ })).toBeEnabled();
    expect((await mockApi.getExecution()).items.every((i) => i.type === "Notebook")).toBe(true);
  }, 90000);
});
