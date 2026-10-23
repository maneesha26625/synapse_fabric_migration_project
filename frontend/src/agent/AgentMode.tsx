import type { ReactNode } from "react";
import { ActivityProvider } from "../state/ActivityState";
import { ConsultProvider } from "./ConsultState";
import { OrchestratorProvider } from "./OrchestratorState";
import "./agent.css";

/**
 * The consultation and the agent orchestrator: steps 2 and 3 of the journey. Their state lives
 * above the router, so the agent keeps working while a person moves between pages.
 */
export function AgentProvider({ children }: { children: ReactNode }) {
  return (
    <ConsultProvider>
      <OrchestratorProvider>
        {/* The application's activity log watches everything, the agent included. */}
        <ActivityProvider>{children}</ActivityProvider>
      </OrchestratorProvider>
    </ConsultProvider>
  );
}
