"use client";

// Sources page — manage the list of tenant backends this browser can read
// from. URL + API key kept in localStorage; the CLI does the same
// (~/.config/rtd/config.toml).
//
// Magic-link UX: the kit's install.sh prints a URL of the form
// `/sources?url=<backend>&name=<label>` so an operator only needs to
// paste their own minted API key. The query params pre-fill the form.

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useSources } from "@/lib/source-context";
import { newSourceId } from "@/lib/sources";

function SourcesPageInner() {
  const {
    ready,
    store,
    upsertSource,
    removeSource,
    setDefaultSource,
  } = useSources();
  const params = useSearchParams();

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [makeDefault, setMakeDefault] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Magic link: ?url=&name= pre-fills the form so an invited tester only
  // needs to paste their key. Runs once per page load.
  useEffect(() => {
    const linkUrl = params.get("url");
    const linkName = params.get("name");
    if (linkUrl) setUrl(linkUrl);
    if (linkName) setName(linkName);
  }, [params]);

  if (!ready) {
    return (
      <p className="text-sm text-muted-foreground">Loading sources…</p>
    );
  }

  const onAdd = (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    const trimmedUrl = url.trim().replace(/\/+$/, "");
    const trimmedKey = apiKey.trim();
    const trimmedName = name.trim();
    if (!trimmedName || !trimmedUrl || !trimmedKey) {
      setError("Name, URL, and API key are all required.");
      return;
    }
    try {
      new URL(trimmedUrl);
    } catch {
      setError(`"${trimmedUrl}" doesn't look like a valid URL.`);
      return;
    }
    upsertSource(
      {
        id: newSourceId(),
        name: trimmedName,
        url: trimmedUrl,
        apiKey: trimmedKey,
      },
      makeDefault || store.sources.length === 0,
    );
    setName("");
    setUrl("");
    setApiKey("");
    setMakeDefault(false);
  };

  const onRemove = (id: string, displayName: string) => {
    if (
      !window.confirm(
        `Remove source "${displayName}"? The tenant data isn't touched — only this browser entry is cleared.`,
      )
    ) {
      return;
    }
    removeSource(id);
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Add source</CardTitle>
          <CardDescription>
            Paste your deployment&apos;s backend URL and a viewer-scoped API
            key minted via the CLI (<code>rtd-cli</code> issues keys against an
            admin token). Keys are stored in this browser&apos;s localStorage.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onAdd} className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="src-name">Name</Label>
              <Input
                id="src-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Acme prod"
                autoComplete="off"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="src-url">Backend URL</Label>
              <Input
                id="src-url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://rtd-prod-backend.azurecontainerapps.io"
                autoComplete="off"
              />
            </div>
            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="src-key">API key</Label>
              <Input
                id="src-key"
                type="password"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="rtd_…"
                autoComplete="off"
              />
            </div>
            <label className="flex items-center gap-2 text-sm sm:col-span-2">
              <input
                type="checkbox"
                checked={makeDefault}
                onChange={(event) => setMakeDefault(event.target.checked)}
                className="h-4 w-4 rounded border-input"
              />
              Make default
            </label>
            {error && (
              <p className="text-sm text-destructive sm:col-span-2">{error}</p>
            )}
            <Button type="submit" className="sm:col-span-2">
              Add source
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Configured sources</CardTitle>
          <CardDescription>
            The currently selected source is what the rest of the app reads
            from. The starred entry is the default on page load.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {store.sources.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No sources yet — add one above.
            </p>
          ) : (
            <ul className="divide-y">
              {store.sources.map((source) => {
                const isDefault = source.id === store.defaultId;
                return (
                  <li
                    key={source.id}
                    className="flex items-center justify-between gap-3 py-3"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-medium">
                          {source.name}
                        </span>
                        {isDefault && (
                          <Badge variant="secondary">default</Badge>
                        )}
                        {source.scope && (
                          <Badge variant="outline">{source.scope}</Badge>
                        )}
                      </div>
                      <p className="truncate text-xs text-muted-foreground">
                        {source.url}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {!isDefault && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setDefaultSource(source.id)}
                        >
                          Make default
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Remove source"
                        onClick={() => onRemove(source.id, source.name)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default function SourcesPage() {
  // useSearchParams() requires a Suspense boundary in static export builds.
  return (
    <Suspense
      fallback={
        <p className="text-sm text-muted-foreground">Loading sources…</p>
      }
    >
      <SourcesPageInner />
    </Suspense>
  );
}
