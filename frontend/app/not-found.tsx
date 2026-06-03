// App-Router-compatible 404 page. Next 15's default falls back to a Pages-
// Router `_document` import that breaks `output: 'export'` builds; providing
// our own not-found bypasses that path.

import Link from "next/link";

export default function NotFound() {
  return (
    <div className="container py-10">
      <h1 className="text-lg font-semibold">Not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        That route doesn&apos;t exist.{" "}
        <Link href="/" className="underline">
          Back to engagements
        </Link>
        .
      </p>
    </div>
  );
}
