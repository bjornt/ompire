import { createContext, useContext, useRef } from "react";

// --- locating the daemon's own validation error --------------------------------
// A refusal comes back addressed the way the author wrote the document
// (`steps[2].prompt.parts[0].value`). Each control knows its own address, so
// the reason appears at the field it is about rather than only in a summary.

export const ProblemContext = createContext<{ location: string; message: string } | null>(
  null,
);

/** True when the daemon's refusal is about this exact field. */
export function useProblem(location: string): string | null {
  const problem = useContext(ProblemContext);
  return problem !== null && problem.location === location ? problem.message : null;
}

/** True when the refusal is about this field or something inside it. */
export function useProblemWithin(location: string): boolean {
  const problem = useContext(ProblemContext);
  if (problem === null) return false;
  return (
    problem.location === location ||
    problem.location.startsWith(`${location}.`) ||
    problem.location.startsWith(`${location}[`)
  );
}

/** Keys that survive a rename and a reorder.
 *
 * Not the row's name: an invalid draft is allowed to contain two steps called
 * the same thing, and keying on the name would collapse them into one card —
 * hiding exactly the mistake the operator opened the editor to fix. */
export function useRowKeys() {
  const keys = useRef<string[]>([]);
  const counter = useRef(0);
  const mint = () => `row-${counter.current++}`;
  return {
    at(index: number): string {
      while (keys.current.length <= index) keys.current.push(mint());
      return keys.current[index];
    },
    inserted(index: number) {
      keys.current.splice(index, 0, mint());
    },
    removed(index: number) {
      keys.current.splice(index, 1);
    },
    moved(from: number, to: number) {
      const [key] = keys.current.splice(from, 1);
      keys.current.splice(to, 0, key);
    },
  };
}
