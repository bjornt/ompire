import type { ReactNode } from "react";
import { ProblemContext, useProblem } from "./problemContext";

/** Carries one located refusal down to the control it is about. */
export function ProblemProvider({
  location,
  message,
  children,
}: {
  location: string | null;
  message: string;
  children: ReactNode;
}) {
  return (
    <ProblemContext.Provider value={location === null ? null : { location, message }}>
      {children}
    </ProblemContext.Provider>
  );
}

export function FieldProblem({ location }: { location: string }) {
  const message = useProblem(location);
  if (message === null) return null;
  return (
    <p className="editorProblem" data-testid={`problem-${location}`}>
      {message}
    </p>
  );
}
