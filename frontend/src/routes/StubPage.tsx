import "./StubPage.css";

export function StubPage({ title }: { title: string }) {
  return (
    <div className="stub" data-testid="stub-page">
      <h1>{title}</h1>
      <p>Not yet built — this view lands in a later ROADMAP chunk.</p>
    </div>
  );
}
