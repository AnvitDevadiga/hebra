import { explorer, type Fragment } from "../lib/data";

export function SourceView({ fragment }: { fragment: Fragment }) {
  const text = explorer.files[fragment.path];
  if (text === undefined) return <p className="note">Source not available.</p>;
  const lines = text.replace(/\n$/, "").split("\n");
  return (
    <>
      <p className="note">
        <code>{fragment.path}</code>, lines {fragment.start_line}-{fragment.end_line}
      </p>
      <pre className="code" aria-label={`Source of ${fragment.path}`}>
        {lines.map((line, i) => {
          const n = i + 1;
          const hit = n >= fragment.start_line && n <= fragment.end_line;
          return (
            <span key={n} className={hit ? "line hit" : "line"}>
              <span className="num">{n}</span>
              {line || " "}
            </span>
          );
        })}
      </pre>
    </>
  );
}
