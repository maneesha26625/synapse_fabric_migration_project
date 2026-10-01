import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/layout/Layout";
import { Assessment } from "./pages/Assessment";
import { Dependencies } from "./pages/Dependencies";
import { Discovery } from "./pages/Discovery";
import { Execute } from "./pages/Execute";
import { FabricTarget } from "./pages/FabricTarget";
import { Plan } from "./pages/Plan";
import { Projects } from "./pages/Projects";
import { SynapseSource } from "./pages/SynapseSource";
import { Validate } from "./pages/Validate";
import { AppStateProvider } from "./state/AppState";
import { MigrationStateProvider } from "./state/MigrationState";

export function App() {
  return (
    <AppStateProvider>
      <MigrationStateProvider>
        <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <a href="#main" className="sr-only">Skip to content</a>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<Projects />} />
              <Route path="synapse" element={<SynapseSource />} />
              <Route path="fabric" element={<FabricTarget />} />
              <Route path="discovery" element={<Discovery />} />
              <Route path="assessment" element={<Assessment />} />
              <Route path="dependencies" element={<Dependencies />} />
              <Route path="plan" element={<Plan />} />
              <Route path="execute" element={<Execute />} />
              <Route path="validate" element={<Validate />} />
              {/* The earlier names keep working. */}
              <Route path="connections" element={<Navigate to="/synapse" replace />} />
              <Route path="execution" element={<Navigate to="/execute" replace />} />
              <Route path="validation" element={<Navigate to="/validate" replace />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </MigrationStateProvider>
    </AppStateProvider>
  );
}
