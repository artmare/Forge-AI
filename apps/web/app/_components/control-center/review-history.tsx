import type { TaskReview } from "./types";
import { StatusBadge } from "./ui";

function reviewTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function ReviewHistory({ reviews }: { reviews: TaskReview[] }) {
  if (!reviews.length) return null;
  return <section className="review-history" aria-labelledby="review-history-title">
    <p id="review-history-title">Review history</p>
    <div>{reviews.map((review) => <article key={review.id}>
      <span className="review-history-rail"/>
      <div className="review-history-heading">
        <div><strong>Iteration {review.iteration}</strong><time dateTime={review.created_at}>{reviewTime(review.created_at)}</time></div>
        <StatusBadge status={review.decision}/>
      </div>
      {review.feedback && <blockquote>{review.feedback}</blockquote>}
    </article>)}</div>
  </section>;
}
