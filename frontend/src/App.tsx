import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { LEGACY_STEP } from "./components/journey/steps";
import { Layout } from "./components/layout/Layout";
import { Setup } from "./pages/Setup";
import { Workspace } from "./pages/Workspace";
import { AppStateProvider } from "./state/AppState";
import { MigrationStateProvider } from "./state/MigrationState";

/** The earlier one-page-per-step addresses open the same step in the migration journey. */
function LegacyStep() {
  const { page = "" } = useParams();
  const step = LEGACY_STEP[page];
  return <Navigate to={step ? `/migration?step=${step}` : "/"} replace />;
}

export function App() {
  return (
    <AppStateProvider>
      <MigrationStateProvider>
        <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <a href="#main" className="sr-only">Skip to content</a>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<Setup />} />
              <Route path="migration" element={<Workspace />} />
              {/* Connection pages are now part of the start page. */}
              <Route path="synapse" element={<Navigate to="/" replace />} />
              <Route path="fabric" element={<Navigate to="/" replace />} />
              <Route path="connections" element={<Navigate to="/" replace />} />
              <Route path=":page" element={<LegacyStep />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </MigrationStateProvider>
    </AppStateProvider>
  );
}
