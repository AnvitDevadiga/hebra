import { REPO, blob } from "../lib/links";

export function Footer() {
  return (
    <footer>
      <div className="container footer-inner">
        <span>Built by Anvit Devadiga.</span>
        <span>
          <a href={REPO}>GitHub</a> · <a href={blob("LICENSE")}>MIT License</a>
        </span>
      </div>
    </footer>
  );
}
