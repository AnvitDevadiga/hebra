// Verifies in-page anchors and repository links without network access:
// every "#id" link has a matching id, and every link into the GitHub repository
// (blob/tree helpers and URLs in the generated data) names a path that exists.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

const site = resolve(import.meta.dirname, "..");
const repoRoot = resolve(site, "..");
const files = readdirSync(join(site, "src"), { recursive: true })
  .filter((f) => f.endsWith(".tsx"))
  .map((f) => readFileSync(join(site, "src", f), "utf8"));
const source = files.join("\n");
const data = ["benchmarks.json", "explorer.json"]
  .map((f) => readFileSync(join(site, "src/data", f), "utf8"))
  .join("\n");

const errors = [];
const ids = new Set([...source.matchAll(/\bid="([\w-]+)"/g)].map((m) => m[1]));
for (const [, anchor] of source.matchAll(/href="#([\w-]+)"/g)) {
  if (!ids.has(anchor)) errors.push(`missing anchor target #${anchor}`);
}
for (const [, href] of source.matchAll(/\["#([\w-]+)",/g)) {
  if (!ids.has(href)) errors.push(`missing nav target #${href}`);
}
const paths = [
  ...[...source.matchAll(/\b(?:blob|tree)\("([^"#]+)/g)].map((m) => m[1]),
  ...[...data.matchAll(/github\.com\/AnvitDevadiga\/hebra\/(?:blob|tree)\/main\/([^"#]+)/g)].map(
    (m) => m[1],
  ),
];
for (const p of new Set(paths)) {
  if (!existsSync(join(repoRoot, p))) errors.push(`repository path does not exist: ${p}`);
}
const html = existsSync(join(site, "dist/index.html"))
  ? readFileSync(join(site, "dist/index.html"), "utf8")
  : "";
if (html && !html.includes('src="/hebra/assets/')) errors.push("built index.html lacks /hebra/ base");

if (errors.length) {
  console.error(errors.join("\n"));
  process.exit(1);
}
console.log(`links ok: ${ids.size} ids, ${new Set(paths).size} repository paths checked`);
