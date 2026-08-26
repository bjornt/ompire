import {
  formatReviewCommentCount,
  formatReviewOutcome,
  formatReviewRecordedAt,
  projectReview,
} from "../lib/reviewPresentation";
import type { ReviewState, SessionInfo } from "../types";
import "./ReviewSummary.css";

export function ReviewSummary({
  review,
  primarySession,
  compact = false,
}: {
  review: ReviewState | undefined;
  primarySession: SessionInfo | undefined;
  compact?: boolean;
}) {
  const presentation = projectReview(review, primarySession);

  return (
    <div
      className={`reviewSummary${compact ? " compact" : ""}`}
      data-testid={compact ? "review-summary-compact" : "review-summary"}
    >
      <div className="reviewSummaryStatus">
        <span className={`reviewStatusBadge ${presentation.state}`}>{presentation.label}</span>
        <span className="reviewSummaryHint">{presentation.hint}</span>
      </div>
      {presentation.url && presentation.state === "open" && (
        <a
          className={`reviewExternalLink${compact ? " reviewPillLink" : ""}`}
          href={presentation.url}
          target="_blank"
          rel="noreferrer"
          data-testid="review-external-link"
        >
          Open llmvet review: {presentation.url}
        </a>
      )}
      {!compact && presentation.iterations.length > 0 && (
        <ol className="reviewIterationList" data-testid="review-iterations">
          {presentation.iterations.map((iteration, index) => {
            const count = formatReviewCommentCount(iteration);
            return (
              <li key={`${iteration.recorded_at}-${index}`} className={`reviewIteration ${iteration.outcome}`}>
                <span className="reviewIterationOutcome">{formatReviewOutcome(iteration.outcome)}</span>
                <time className="reviewIterationTime" dateTime={iteration.recorded_at}>
                  {formatReviewRecordedAt(iteration.recorded_at)}
                </time>
                {count && <span className="reviewIterationCount">{count}</span>}
                {iteration.stderr && (
                  <details className="reviewIterationError">
                    <summary>Show error details</summary>
                    <pre>{iteration.stderr}</pre>
                  </details>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
