import type { StepRecord, WorkflowState } from "../types";

/** Every recorded attempt at one declared step, in the order they happened.
 *
 * A step with several visits has several records, and each one is separately
 * inspectable: a rejected fix and the corrected one are two attempts, and
 * showing only the latest would erase the rejection from the run's history.
 */
export function attemptsFor(
  workflow: WorkflowState | null,
  step: string | null,
): StepRecord[] {
  if (workflow === null || step === null) return [];
  return workflow.steps.filter((record) => record.step === step);
}
