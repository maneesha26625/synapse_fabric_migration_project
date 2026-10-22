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
});

describe("agent mode", () => {
  it("offers the consultation and the orchestrator at the foot of the explorer, and goes back to the steps", async () => {
    const user = userEvent.setup();
    go("/migration?step=discover");
    const launch = await screen.findByRole("group", { name: "Agent mode" });
    const consult = within(launch).getByRole("button", { name: /Consultation/ });
    await waitFor(() => expect(consult).toBeEnabled(), { timeout: 5000 });
    await user.click(consult);
    expect(await screen.findByRole("heading", { level: 2, name: "Consultation" })).toBeInTheDocument();
    // The steps stay on top; the step panel gives way to the consultation.
    expect(screen.getByRole("navigation", { name: "Migration steps" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: "Discover" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Back to the steps" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Discover" })).toBeInTheDocument();
  }, 20000);

  it("recommends from discovery, needs a person for the sign-off, and approves a version of the brief", async () => {
    const user = userEvent.setup();
    go("/migration?step=discover&view=consult");
    const question =await screen.findByRole("heading", { level: 3, name: "Which kinds of objects should this migration move?" }, { timeout: 8000 });
    const card = question.closest("section")!;
    expect(within(card).getByText(/^Recommended:/)).toBeInTheDocument();

    // Typed text is matched to the question's options without an AI model.
    await user.type(screen.getByRole("textbox", { name: "Ask or describe" }), "?{Enter}");
    expect(await screen.findByText(/Suggested:/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Review & approve/ }));
    // The sign-off has no recommendation: it holds the brief back until a person answers it.
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Answer it" }));
    await user.type(await screen.findByRole("textbox", { name: "Who signs off this migration for the client?" }), "Priya Shah, data lead");
    await user.click(screen.getByRole("button", { name: /Save answer/ }));

    await user.click(screen.getByRole("button", { name: /Review & approve/ }));
    await user.click(screen.getByRole("button", { name: "Accept remaining recommendations" }));
    await user.click(screen.getByRole("button", { name: "Submit for approval" }));
    expect(await screen.findByText(/Version 1 is submitted and waits for approval/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Approved by" })).toHaveValue("Priya Shah, data lead");
    await user.click(screen.getByRole("button", { name: /Approve version 1/ }));
    expect(await screen.findByText(/Version 1 is approved by Priya Shah, data lead/)).toBeInTheDocument();

    // Changing an answer afterwards means a new version must be approved.
    await user.click(screen.getByRole("button", { name: /Scope: what moves/ }));
    await user.click(screen.getAllByRole("button", { name: "Change" })[0]);
    const scope = await screen.findByRole("group", { name: "Which kinds of objects should this migration move?" });
    await user.click(within(scope).getAllByRole("checkbox")[0]);
    await user.click(screen.getByRole("button", { name: /Save answer/ }));
    await user.click(screen.getByRole("button", { name: /Review & approve/ }));
    expect(await screen.findByText(/The answers changed since the last approved version/)).toBeInTheDocument();
    expect(stored(CONSULT).versions).toHaveLength(1);
  }, 40000);

  it("turns the approved answers into the run's stages and options", async () => {
    const graph = await mockApi.getDependencies();
    const capabilities = await mockApi.getCapabilities();
    const facts = readFacts({ discovery: await mockApi.getDiscoveryStatus(), connection: await mockApi.getConnection(), graph, capabilities, fabricConnected: true, fabricGit: null });
    const s = settingsFrom(facts, resolve(facts, { "scope.stages": ["notebooks", "warehouse"], "data.move": "none", "val.retry": "stop", "target.collation": "case_sensitive" }));
    expect(s.stages).toEqual(["warehouse", "notebooks"]);
    expect(s.options).toMatchObject({ stopOnFailure: true, collation: "case_sensitive", stages: ["warehouse", "notebooks"] });
    expect(s.planTypes).toContain("Notebook");
    expect(s.planTypes).not.toContain("Pipeline");
  });

  it("runs the approved brief end to end: plan, approval, run, validation, hand-over", async () => {
    const user = userEvent.setup();
    // A small scope keeps the demo run short: notebooks only, no data, no connections.
    localStorage.setItem(CONSULT, JSON.stringify({
      answers: { "scope.stages": ["notebooks"], "data.move": "none", "sec.credentials": "skip", "ops.schedules": "skip", "sec.roles": "no", "context.owner": "Test lead" },
      notes: [], versions: [],
    }));
    go("/migration?step=discover&view=consult");
    await user.click(await screen.findByRole("button", { name: /Review & approve/ }, { timeout: 8000 }));
    await user.click(await screen.findByRole("button", { name: "Submit for approval" }));
    await user.click(await screen.findByRole("button", { name: /Approve version 1/ }));
    await user.click(await screen.findByRole("button", { name: /Continue/ }));

    expect(await screen.findByRole("heading", { level: 2, name: "Agent orchestrator" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Start the agent/ }));
    // The brief asks for approval before writing: the agent stops there.
    const approve = await screen.findByRole("button", { name: "Approve and start the run" }, { timeout: 15000 });
    expect(screen.getByRole("group", { name: /Approve writing to/ })).toBeInTheDocument();
    await user.click(approve);
    const stages = screen.getByRole("navigation", { name: "Agent stages" });
    await waitFor(() => expect(within(stages).getByRole("button", { name: /Hand-over/ })).toHaveTextContent(/Done|Every object/), { timeout: 60000 });
    expect(within(stages).getByRole("button", { name: /Migration run/ })).toHaveTextContent(/created/);
    expect(within(stages).getByRole("button", { name: /Validation/ })).toHaveTextContent(/checks/);
    // The six steps show what the agent did.
    expect(stored(JOURNEY)).toEqual(["discover", "assess", "waves", "plan", "migrate", "validate"]);
    expect((await mockApi.getExecution()).items.every((i) => i.type === "Notebook")).toBe(true);
  }, 90000);
});
