import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { DaemonProvider } from "./lib/daemonSocket";
import { Chrome } from "./components/Chrome";
import { TasksView } from "./routes/TasksView";
import { TaskDetailView } from "./routes/TaskDetailView";
import { ShipFlowView } from "./routes/ShipFlowView";
import { ShipIndexView } from "./routes/ShipIndexView";
import { NotFoundView } from "./routes/NotFoundView";
import { SpawnView } from "./routes/SpawnView";
import { ProjectsView } from "./routes/ProjectsView";
import { SettingsView } from "./routes/SettingsView";

export function App() {
  return (
    <DaemonProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Chrome />}>
            <Route path="/" element={<Navigate to="/tasks" replace />} />
            <Route path="/tasks" element={<TasksView />} />
            <Route path="/tasks/:id" element={<TaskDetailView />} />
            <Route path="/projects" element={<ProjectsView />} />
            <Route path="/spawn" element={<SpawnView />} />
            <Route path="/ship" element={<ShipIndexView />} />
            <Route path="/ship/:id" element={<ShipFlowView />} />
            <Route path="/settings" element={<SettingsView />} />
            <Route path="*" element={<NotFoundView />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </DaemonProvider>
  );
}

export default App;
