import { Link } from "react-router-dom";

export function NotFoundView() {
  return (
    <div className="empty" data-testid="app-not-found">
      <strong>Page not found</strong>
      <span>The address does not match an Ompire view.</span>
      <span>
        <Link to="/tasks">Tasks</Link> · <Link to="/ship">Ship flow</Link>
      </span>
    </div>
  );
}
